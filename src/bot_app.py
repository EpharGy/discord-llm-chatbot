import asyncio
import os
from dotenv import load_dotenv
from .config_service import ConfigService
from .logger_factory import get_logger, configure_logging
from .discord_client_adapter import DiscordClientAdapter
from .message_router import MessageRouter
from .persona_service import PersonaService
from .prompt_template_engine import PromptTemplateEngine
from .tokenizer_service import TokenizerService
from .conversation_memory import ConversationMemory
from .participation_policy import ParticipationPolicy
from .task_queue import MentionsQueue
from .llm.openrouter_client import OpenRouterClient
from .llm.openai_compat_client import OpenAICompatClient
from .llm.multi_backend_client import ContextualMultiBackendClient
from .conversation_batcher import ConversationBatcher
from .lore_service import LoreService


async def main() -> None:
    # Ensure env + config
    try:
        if not os.path.exists(".env") and os.path.exists(".env.example"):
            with open(".env.example", "r", encoding="utf-8") as s, open(".env", "w", encoding="utf-8") as d:
                d.write(s.read())
    except Exception:
        pass
    load_dotenv()
    try:
        if not os.path.exists("config.yaml") and os.path.exists("config.example.yaml"):
            with open("config.example.yaml", "r", encoding="utf-8") as s, open("config.yaml", "w", encoding="utf-8") as d:
                d.write(s.read())
    except Exception:
        pass

    config = ConfigService("config.yaml")
    configure_logging(
        level=config.log_level(),
        tz=None,
        fmt="text",
        lib_log_level=config.lib_log_level(),
        console_to_file=config.log_console(),
        error_file=config.log_errors(),
    )
    logger = get_logger("bot_app")

    # Core services
    persona = PersonaService(config.persona_path())
    template_engine = PromptTemplateEngine(
        system_prompt_path=config.system_prompt_path(),
        context_template_path=config.context_template_path(),
        persona_service=persona,
    )
    tokenizer = TokenizerService()
    memory = ConversationMemory()
    policy = ParticipationPolicy(config.rate_limits(), config.participation())
    policy.set_window_size(config.window_size())
    queue = MentionsQueue()
    batcher = ConversationBatcher()
    prev_conv_active: dict[str, bool] = {}
    # Persona diagnostics
    try:
        for w in (config.persona_diagnostics() or []):
            logger.warning(w)
    except Exception:
        pass

    model_cfg = config.model()
    # Build providers based on config
    providers = []
    nsfw_providers = []
    vision_providers = []
    # OpenRouter client (default)
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
    # Optional OpenAI-compatible local backend
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
        logger.info(f"openai-compat-client-enabled url={_oai_url}")
        providers.append(oai)
        nsfw_providers.insert(0, oai)
        vision_providers.append(oai)

    # If config specifies explicit provider order per context, respect it
    order = (model_cfg.get("provider_order") or {}) if isinstance(model_cfg, dict) else {}
    def order_list(kind: str, current: list):
        names = [n.strip().lower() for n in (order.get(kind) or [])]
        by_name = {"openrouter": orc, "openai": oai}
        out = []
        for n in names:
            c = by_name.get(n)
            if c is not None:
                out.append(c)
        # Append any not listed
        for c in current:
            if c not in out:
                out.append(c)
        return out
    providers = order_list("normal", providers)
    nsfw_providers = order_list("nsfw", nsfw_providers)
    vision_providers = order_list("vision", vision_providers)
    web_providers = order_list("web", providers)

    llm = ContextualMultiBackendClient(normal=providers, nsfw=nsfw_providers, vision=vision_providers, web=web_providers)
    lore = LoreService(config.lore_paths(), md_priority=config.lore_md_priority()) if config.lore_enabled() else None

    router = MessageRouter(
        template_engine=template_engine,
        tokenizer=tokenizer,
        memory=memory,
        policy=policy,
        logger=get_logger("MessageRouter"),
        mentions_queue=queue,
        batcher=batcher,
        llm=llm,
        model_cfg=model_cfg,
        lore=lore,
        lore_config={
            "enabled": config.lore_enabled(),
            "max_fraction": config.lore_max_fraction(),
        },
    )

    # Determine run mode: DISCORD | WEB | BOTH
    mode = config.bot_method()
    port = config.html_port()
    host = config.html_host()
    # Prepare optional Discord client
    token = os.getenv("DISCORD_TOKEN")
    client = None
    if mode in ("DISCORD", "BOTH"):
        if not token:
            raise RuntimeError("Missing DISCORD_TOKEN in environment")
        intents_cfg = config.discord_intents()
        client = DiscordClientAdapter(router=router, intents_cfg=intents_cfg, logger=get_logger("Discord"))

    async def process_mentions_loop():
        while True:
            try:
                if client is None:
                    await asyncio.sleep(2)
                    continue
                for cid in queue.channels():
                    if memory.responses_in_window(cid, policy.window_seconds) < policy.max_responses:
                        item = queue.pop(cid)
                        if item:
                            try:
                                import discord
                                channel = await client.fetch_channel(int(cid))
                                if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                                    msg = await channel.fetch_message(item.message_id)
                                    await router.handle_message(msg)
                            except Exception:
                                pass
            except Exception:
                pass
            await asyncio.sleep(2)

    async def process_batches_loop():
        while True:
            batch_interval = 10
            batch_limit = 10
            try:
                batch_interval = int(config.conversation_batch_interval_seconds())
                batch_limit = int(config.conversation_batch_limit())
                for cid in batcher.channels():
                    ch_id = str(cid)
                    active = memory.conversation_mode_active(ch_id)
                    was_active = prev_conv_active.get(ch_id, False)
                    # Fetch channel for NSFW detection
                    channel_obj = None
                    try:
                        if client is not None:
                            import discord
                            channel_obj = await client.fetch_channel(int(ch_id))
                    except Exception:
                        channel_obj = None
                    if active:
                        events = batcher.drain(ch_id, limit=batch_limit)
                        if events:
                            # Pass allow_outside_window=True to avoid double consumption;
                            # budget was already reduced when messages were admitted by conversation_mode_adjust.
                            # Use enhanced batch builder that accepts channel for NSFW + overrides
                            reply = await router.build_batch_reply(cid=ch_id, events=events, channel=channel_obj, allow_outside_window=True)
                            if reply and channel_obj is not None:
                                try:
                                    import discord
                                    if isinstance(channel_obj, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                                        sent = await channel_obj.send(reply)
                                    else:
                                        sent = None
                                    if sent:
                                        memory.record({
                                            "channel_id": ch_id,
                                            "author_id": str(getattr(sent.author, 'id', '0')),
                                            "content": reply,
                                            "is_bot": True,
                                            "created_at": getattr(sent, 'created_at', None),
                                            "author_name": getattr(sent.author, 'display_name', 'bot'),
                                        })
                                    else:
                                        get_logger("ConversationBatcher").debug(f"batch-send-failed channel={ch_id}")
                                except Exception:
                                    get_logger("ConversationBatcher").debug(f"batch-error channel={ch_id}")
                            else:
                                get_logger("ConversationBatcher").debug(f"batch-no-reply channel={ch_id} events={len(events)}")
                    else:
                        if was_active:
                            events = batcher.drain(ch_id, limit=batch_limit)
                            if events:
                                reply = await router.build_batch_reply(cid=ch_id, events=events, channel=channel_obj, allow_outside_window=True)
                                if reply and channel_obj is not None:
                                    try:
                                        import discord
                                        if isinstance(channel_obj, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                                            sent = await channel_obj.send(reply)
                                        else:
                                            sent = None
                                        if sent:
                                            memory.record({
                                                "channel_id": ch_id,
                                                "author_id": str(getattr(sent.author, 'id', '0')),
                                                "content": reply,
                                                "is_bot": True,
                                                "created_at": getattr(sent, 'created_at', None),
                                                "author_name": getattr(sent.author, 'display_name', 'bot'),
                                            })
                                    except Exception:
                                        pass
                        batcher.clear(ch_id)
                    prev_conv_active[ch_id] = active
            except Exception:
                pass
            await asyncio.sleep(max(1, int(batch_interval)))

    # Build run tasks based on mode
    tasks: list = []
    server = None
    if mode in ("WEB", "BOTH"):
        from .http_app import create_app
        import uvicorn
        app = create_app()
        config_uv = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config_uv)
        tasks.append(server.serve())
        logger.info(f"Web server: http://{host}:{port}")
    if mode in ("DISCORD", "BOTH") and client is not None and token:
        tasks.extend([client.start(token), process_mentions_loop(), process_batches_loop()])
        logger.info("Discord bot starting…")

    if not tasks:
        logger.info("No run tasks scheduled (check bot_type.method). Exiting.")
        return

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
