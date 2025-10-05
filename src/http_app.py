from __future__ import annotations

import asyncio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .config_service import ConfigService
from .logger_factory import configure_logging, get_logger
from .message_router import MessageRouter
from .persona_service import PersonaService
from .prompt_template_engine import PromptTemplateEngine
from .tokenizer_service import TokenizerService
from .conversation_memory import ConversationMemory
from .participation_policy import ParticipationPolicy
from .llm.openrouter_client import OpenRouterClient
from .llm.kobold_openai_client import KoboldOpenAIClient
from .llm.multi_backend_client import ContextualMultiBackendClient
from .conversation_batcher import ConversationBatcher
from .lore_service import LoreService


class ChatIn(BaseModel):
    user_id: str = "web-user"
    user_name: str = "web-user"
    channel_id: str | None = None
    content: str


def build_router_from_config(cfg: ConfigService) -> MessageRouter:
    persona = PersonaService(cfg.persona_path())
    tmpl = PromptTemplateEngine(cfg.system_prompt_path(), cfg.context_template_path(), persona)
    tok = TokenizerService()
    mem = ConversationMemory()
    policy = ParticipationPolicy(cfg.rate_limits(), cfg.participation())
    policy.set_window_size(cfg.window_size())
    batcher = ConversationBatcher()
    model_cfg = cfg.model()
    # Build providers
    providers = []
    nsfw_providers = []
    vision_providers = []
    orc = None
    try:
        orc = OpenRouterClient(
            concurrency=int(model_cfg.get("concurrency", 2)),
            base_url=model_cfg.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
            retry_attempts=int(model_cfg.get("retry_attempts", 2)),
            http_referer=model_cfg.get("http_referer", "http://example.com"),
            x_title=model_cfg.get("x_title", "Discord LLM Bot"),
        )
        providers.append(orc)
        nsfw_providers.append(orc)
        vision_providers.append(orc)
    except Exception:
        pass
    kob = None
    kob_cfg = (model_cfg.get("kobold") or {}) if isinstance(model_cfg, dict) else {}
    if kob_cfg.get("enabled", False):
        _kob_url = str(kob_cfg.get("base_url", "http://127.0.0.1:5001/v1/chat/completions"))
        _u = _kob_url.rstrip("/")
        if _u.endswith("/v1"):
            _kob_url = _u + "/chat/completions"
        kob = KoboldOpenAIClient(
            base_url=_kob_url,
            concurrency=int(kob_cfg.get("concurrency", model_cfg.get("concurrency", 2))),
            timeout=float(kob_cfg.get("timeout", 60.0)),
            retry_attempts=int(kob_cfg.get("retry_attempts", 1)),
        )
        get_logger("http_app").info(f"kobold-client-enabled url={_kob_url}")
        providers.append(kob)
        nsfw_providers.insert(0, kob)
        vision_providers.append(kob)

    order = (model_cfg.get("provider_order") or {}) if isinstance(model_cfg, dict) else {}
    def order_list(kind: str, current: list):
        names = [n.strip().lower() for n in (order.get(kind) or [])]
        by_name = {"openrouter": orc, "kobold": kob}
        out = []
        for n in names:
            c = by_name.get(n)
            if c is not None:
                out.append(c)
        for c in current:
            if c not in out:
                out.append(c)
        return out
    providers = order_list("normal", providers)
    nsfw_providers = order_list("nsfw", nsfw_providers)
    vision_providers = order_list("vision", vision_providers)
    web_providers = order_list("web", providers)

    llm = ContextualMultiBackendClient(normal=providers, nsfw=nsfw_providers, vision=vision_providers, web=web_providers)
    lore = LoreService(cfg.lore_paths(), md_priority=cfg.lore_md_priority()) if cfg.lore_enabled() else None
    router = MessageRouter(
        template_engine=tmpl,
        tokenizer=tok,
        memory=mem,
        policy=policy,
        logger=get_logger("WebRouter"),
        mentions_queue=None,
        batcher=batcher,
        llm=llm,
        model_cfg=model_cfg,
        lore=lore,
        lore_config={"enabled": cfg.lore_enabled(), "max_fraction": cfg.lore_max_fraction()},
    )
    return router


def create_app() -> FastAPI:
    cfg = ConfigService("config.yaml")
    configure_logging(level=cfg.log_level(), tz=None, fmt="text", lib_log_level=cfg.lib_log_level(), console_to_file=cfg.log_console(), error_file=cfg.log_errors())
    log = get_logger("http_app")
    router = build_router_from_config(cfg)
    bearer = cfg.http_auth_bearer_token()
    # Derive a friendly bot label from participation.name_aliases (first alias, stripped of leading @)
    try:
        aliases = cfg.participation().get("name_aliases", []) or []
        bot_label = None
        for a in aliases:
            if isinstance(a, str) and a.strip():
                bot_label = a.strip().lstrip("@")
                break
        if not bot_label:
            bot_label = "Bot"
    except Exception:
        bot_label = "Bot"

    app = FastAPI(title="Discord LLM Bot — Web")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    # Static assets
    try:
        app.mount("/static", StaticFiles(directory="src/web/static"), name="static")
    except Exception:
        pass

    async def home():
        return FileResponse("src/web/static/index.html")

    # Explicitly register root routes (some environments are picky about stacked decorators within a factory)
    app.add_api_route("/", home, methods=["GET"])
    app.add_api_route("/index", home, methods=["GET"])
    app.add_api_route("/index.html", home, methods=["GET"])

    # remove inline /app.js; static serves it

    @app.get("/web-config")
    async def web_config():
        return {"bot_name": bot_label, "default_user_name": "You", "token_required": bool(bearer)}

    @app.post("/chat")
    async def chat(inp: ChatIn, request: Request):
        # Build a minimal event and use the batch reply path to bypass participation policy.
        import datetime
        from types import SimpleNamespace
        # Auth if configured
        if bearer:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth.split(" ",1)[1].strip() != bearer:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        ch_id = (inp.channel_id or "web-room").strip() or "web-room"
        now = datetime.datetime.now(datetime.timezone.utc)
        # per-request correlation id
        mid = f"web-{int(now.timestamp()*1000)}"
        try:
            from .utils.correlation import make_correlation_id
            correlation_id = make_correlation_id(ch_id, mid)
        except Exception:
            correlation_id = f"{ch_id}-web"
        event = {
            "channel_id": ch_id,
            "channel_name": "web-room",
            "author_id": inp.user_id,
            "message_id": mid,
            "content": inp.content,
            "mentions": [],
            "is_bot": False,
            "created_at": now,
            "author_name": inp.user_name,
            "guild_name": "WEB",
            "correlation_id": correlation_id,
        }
        try:
            # Record user event for context
            router.memory.record(event)
            # NSFW=true channel stub
            channel_obj = SimpleNamespace(id=ch_id, name="web-room", nsfw=True, parent=None)
            # Tag web context for provider selection
            event['web'] = True
            reply = await router.build_batch_reply(cid=ch_id, events=[event], channel=channel_obj, allow_outside_window=True)
            reply = reply or ""
            if reply:
                router.memory.record({
                    "channel_id": ch_id,
                    "author_id": "bot",
                    "content": reply,
                    "is_bot": True,
                    "created_at": datetime.datetime.now(datetime.timezone.utc),
                    "author_name": bot_label,
                })
        except Exception as e:
            log.error(f"web-chat-error {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return {"reply": reply}

    @app.post("/reset")
    async def reset_chat():
        """Clear web chat history/state for the single web room."""
        try:
            ch_id = "web-room"
            # Clear memory and batch buffers
            try:
                router.memory.clear(ch_id)
            except Exception:
                pass
            try:
                b = getattr(router, 'batcher', None)
                if isinstance(b, ConversationBatcher):
                    b.clear(ch_id)
            except Exception:
                pass
            return {"ok": True}
        except Exception as e:
            log.error(f"web-reset-error {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # Log available routes for debugging
    try:
        paths = sorted({getattr(r, 'path', '') for r in app.routes})
        log.info(f"http-routes {paths}")
    except Exception:
        pass

    return app
