from __future__ import annotations

import asyncio
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from .config_service import ConfigService
from .logger_factory import configure_logging, get_logger
from .message_router import MessageRouter
from .persona_service import PersonaService
from .prompt_template_engine import PromptTemplateEngine
from .tokenizer_service import TokenizerService
from .conversation_memory import ConversationMemory
from .participation_policy import ParticipationPolicy
from .llm.openrouter_client import OpenRouterClient
from .llm.openai_compat_client import OpenAICompatClient
from .llm.multi_backend_client import ContextualMultiBackendClient
from .conversation_batcher import ConversationBatcher
from .lore_service import LoreService
from .web_room_store import WebRoomStore


class ChatIn(BaseModel):
    user_id: str = "web-user"
    user_name: str = "web-user"
    channel_id: str | None = None
    content: str
    provider: str | None = None  # 'openrouter' | 'openai'
    passcode: str | None = None


class RoomSummary(BaseModel):
    room_id: str
    name: str
    last_active: str
    locked: bool
    provider: str | None = None


class RoomCreateIn(BaseModel):
    name: str
    passcode: str | None = None


class RoomJoinIn(BaseModel):
    passcode: str | None = None
    provider: str | None = None


class RoomJoinOut(BaseModel):
    room: RoomSummary
    messages: list[dict]


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
    oai = None
    oai_cfg = (model_cfg.get("openai") or {}) if isinstance(model_cfg, dict) else {}
    if oai_cfg.get("enabled", False):
        _oai_url = str(oai_cfg.get("base_url", "http://127.0.0.1:5001/v1/chat/completions"))
        _u = _oai_url.rstrip("/")
        if _u.endswith("/v1"):
            _oai_url = _u + "/chat/completions"
        oai = OpenAICompatClient(
            base_url=_oai_url,
            concurrency=int(oai_cfg.get("concurrency", model_cfg.get("concurrency", 2))),
            timeout=float(oai_cfg.get("timeout", 60.0)),
            retry_attempts=int(oai_cfg.get("retry_attempts", 1)),
        )
        get_logger("http_app").info(f"openai-compat-client-enabled url={_oai_url}")
        providers.append(oai)
        nsfw_providers.insert(0, oai)
        vision_providers.append(oai)

    order = (model_cfg.get("provider_order") or {}) if isinstance(model_cfg, dict) else {}
    def order_list(kind: str, current: list):
        names = [n.strip().lower() for n in (order.get(kind) or [])]
        by_name = {"openrouter": orc, "openai": oai}
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
    # Room store (persist web chat transcripts)
    room_store = WebRoomStore(Path(__file__).resolve().parent / "web" / "data")

    def _room_summary(meta) -> RoomSummary:
        return RoomSummary(
            room_id=meta.room_id,
            name=meta.name,
            last_active=meta.last_active,
            locked=meta.requires_passcode,
            provider=getattr(meta, "provider", None),
        )
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

    def _available_providers_from_router() -> list[str]:
        names: set[str] = set()
        try:
            l = getattr(router, 'llm', None)
            if l is not None:
                pools = []
                for attr in ("normal", "nsfw", "vision", "web"):
                    pools.extend(getattr(l, attr, []) or [])
                for p in pools:
                    cls = p.__class__.__name__
                    if cls == 'OpenRouterClient':
                        names.add('openrouter')
                    elif cls == 'OpenAICompatClient':
                        names.add('openai')
        except Exception:
            pass
        return sorted(names)

    @app.get("/rooms")
    async def list_rooms():
        metas = sorted(room_store.list_rooms(), key=lambda m: m.last_active, reverse=True)
        rooms = [_room_summary(meta).model_dump() for meta in metas]
        return {"rooms": rooms}

    @app.post("/rooms", response_model=RoomSummary)
    async def create_room(payload: RoomCreateIn):
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="Room name is required")
        try:
            meta = room_store.create_room(payload.name, payload.passcode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        log.info(f"room-create {meta.room_id}")
        return _room_summary(meta)

    @app.post("/rooms/{room_id}/join", response_model=RoomJoinOut)
    async def join_room(room_id: str, payload: RoomJoinIn):
        meta = room_store.get_room(room_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Room not found")
        if not room_store.validate_passcode(room_id, payload.passcode):
            raise HTTPException(status_code=403, detail="Invalid passcode")
        if payload.provider:
            room_store.set_provider(room_id, payload.provider)
        msgs = room_store.load_messages(room_id)
        return RoomJoinOut(room=_room_summary(meta), messages=msgs)

    @app.get("/web-config")
    async def web_config():
        provs = _available_providers_from_router()
        # Prefer openrouter as default if available, unless user persisted choice on client
        default_provider = 'openrouter' if 'openrouter' in provs else ('openai' if 'openai' in provs else None)
        return {"bot_name": bot_label, "default_user_name": "You", "token_required": bool(bearer), "providers": provs, "default_provider": default_provider}

    @app.post("/chat")
    async def chat(inp: ChatIn, request: Request):
        # Build a minimal event and use the batch reply path to bypass participation policy.
        import datetime
        from types import SimpleNamespace

        # Auth if configured
        if bearer:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth.split(" ", 1)[1].strip() != bearer:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)

        ch_id = (inp.channel_id or "").strip()
        if not ch_id:
            raise HTTPException(status_code=400, detail="room_id required")
        meta = room_store.get_room(ch_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Room not found")
        if not room_store.validate_passcode(ch_id, inp.passcode):
            raise HTTPException(status_code=403, detail="Invalid passcode")
        if inp.provider:
            room_store.set_provider(ch_id, inp.provider)

        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat()

        # per-request correlation id
        mid = f"web-{int(now.timestamp()*1000)}"
        try:
            from .utils.correlation import make_correlation_id

            correlation_id = make_correlation_id(ch_id, mid)
        except Exception:
            correlation_id = f"{ch_id}-web"

        event = {
            "channel_id": ch_id,
            "channel_name": meta.name or ch_id,
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
            room_store.append_message(
                ch_id,
                {
                    "id": mid,
                    "role": "user",
                    "author_id": inp.user_id,
                    "author_name": inp.user_name,
                    "content": inp.content,
                    "created_at": now_iso,
                },
            )
            # NSFW=true channel stub
            channel_obj = SimpleNamespace(id=ch_id, name="web-room", nsfw=True, parent=None)
            # Tag web context for provider selection
            event['web'] = True
            # Optional provider override from UI
            try:
                if inp.provider and inp.provider.lower() in ("openrouter", "openai"):
                    event['provider'] = inp.provider.lower()
            except Exception:
                pass

            reply = await router.build_batch_reply(cid=ch_id, events=[event], channel=channel_obj, allow_outside_window=True)
            reply = reply or ""
            if reply:
                now_bot = datetime.datetime.now(datetime.timezone.utc)
                router.memory.record({
                    "channel_id": ch_id,
                    "author_id": "bot",
                    "content": reply,
                    "is_bot": True,
                    "created_at": now_bot,
                    "author_name": bot_label,
                })
                room_store.append_message(
                    ch_id,
                    {
                        "id": f"{mid}-bot",
                        "role": "assistant",
                        "author_id": "bot",
                        "author_name": bot_label,
                        "content": reply,
                        "created_at": now_bot.isoformat(),
                    },
                )
        except Exception as e:
            log.error(f"web-chat-error {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return {"reply": reply}

    @app.post("/reset")
    async def reset_chat(request: Request):
        """Clear history/state for a specific room."""
        try:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            ch_id = (payload.get("room_id") or "").strip()
            if not ch_id:
                raise HTTPException(status_code=400, detail="room_id required")
            meta = room_store.get_room(ch_id)
            if meta is None:
                raise HTTPException(status_code=404, detail="Room not found")
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
            try:
                room_store.clear_room(ch_id)
            except Exception:
                pass
            return {"ok": True}
        except HTTPException:
            raise
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
