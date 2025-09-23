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
from .conversation_batcher import ConversationBatcher
from .lore_service import LoreService


async def main() -> None:
    # Load .env
    # If first-run, auto-create .env from example (do not overwrite existing)
    try:
        if not os.path.exists(".env") and os.path.exists(".env.example"):
            with open(".env.example", "r", encoding="utf-8") as src, open(".env", "w", encoding="utf-8") as dst:
                dst.write(src.read())
    except Exception:
        pass
    load_dotenv()

    # Ensure config exists; if missing, create from example once
    try:
        if not os.path.exists("config.yaml") and os.path.exists("config.example.yaml"):
            with open("config.example.yaml", "r", encoding="utf-8") as src, open("config.yaml", "w", encoding="utf-8") as dst:
                dst.write(src.read())
    except Exception:
        pass
    # Load config first, then configure logging based on config
    config = ConfigService("config.yaml")
    log_level = config.log_level()
    lib_log_level = config.lib_log_level()
    # Optional future: LOG_FORMAT from config/env
    configure_logging(level=log_level, tz=None, fmt="text", lib_log_level=lib_log_level, log_errors=config.log_errors())
    logger = get_logger(__name__)

    # Init services
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

    # LLM client
    model_cfg = config.model()
    llm = OpenRouterClient(
        concurrency=int(model_cfg.get("concurrency", 2)),
        base_url=model_cfg.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
        retry_attempts=int(model_cfg.get("retry_attempts", 2)),
        http_referer=model_cfg.get("http_referer", "http://example.com"),
        x_title=model_cfg.get("x_title", "Discord LLM Bot"),
    )

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
        }
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN in environment")

    intents_cfg = config.discord_intents()
    client = DiscordClientAdapter(router=router, intents_cfg=intents_cfg, logger=get_logger("Discord"))

    logger.info("Starting Discord bot…")
    async def process_mentions_loop():
        while True:
            # Iterate over channels with queued mentions
            for cid in queue.channels():
                # Check anti-spam window before processing
                if memory.responses_in_window(cid, policy.window_seconds) < policy.max_responses:
                    item = queue.pop(cid)
                    if item:
                        # Fetch message by ID and process as a mention reply
                        try:
                            import discord
                            channel = await client.fetch_channel(int(cid))
                            # Process only channel types known to support fetch_message
                            if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                                msg = await channel.fetch_message(item.message_id)
                                await router.handle_message(msg)
                        except Exception:
                            # Swallow errors; could log and continue
                            pass
            await asyncio.sleep(2)

    try:
        async def process_batches_loop():
            while True:
                batch_interval = 10
                batch_limit = 10
                try:
                    # Every 10s, for channels in conversation mode, drain up to 10 events and ask router to produce a single reply
                    batch_interval = int(config.conversation_batch_interval_seconds())
                    batch_limit = int(config.conversation_batch_limit())
                    for cid in batcher.channels():
                        if memory.conversation_mode_active(str(cid)):
                            events = batcher.drain(str(cid), limit=batch_limit)
                            if events:
                                reply = await router.build_batch_reply(cid=str(cid), events=events)
                                if reply:
                                    try:
                                        import discord
                                        channel = await client.fetch_channel(int(cid))
                                        if isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
                                            sent = await channel.send(reply)
                                        else:
                                            sent = None
                                        if sent:
                                            memory.record({
                                                "channel_id": str(cid),
                                                "author_id": str(getattr(sent.author, 'id', '0')),
                                                "content": reply,
                                                "is_bot": True,
                                                "created_at": sent.created_at if getattr(sent, 'created_at', None) else None,
                                                "author_name": getattr(sent.author, 'display_name', 'bot'),
                                            })
                                    except Exception:
                                        pass
                        else:
                            batcher.clear(str(cid))
                except Exception:
                    pass
                await asyncio.sleep(max(1, int(batch_interval)))

        await asyncio.gather(
            client.start(token),
            process_mentions_loop(),
            process_batches_loop(),
        )
    finally:
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
