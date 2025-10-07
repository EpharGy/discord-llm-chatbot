import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import re
from datetime import datetime, timedelta
import logging

try:
    from src.tool_bridge import ToolBridge
except ImportError:
    # Fallback when PYTHONPATH includes 'src'
    try:
        from tool_bridge import ToolBridge
    except ImportError:
        ToolBridge = None  # type: ignore

REMINDER_FILE = os.path.join(os.path.dirname(__file__), 'reminders.json')
CHECK_INTERVAL = 30  # seconds

TIME_PATTERN = re.compile(r'((?P<days>\d+)d)?((?P<hours>\d+)h)?((?P<minutes>\d+)m)?')

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def now_local() -> datetime:
    return datetime.now().astimezone()


def ensure_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=now_local().tzinfo)
    return dt.astimezone()


def format_timestamp(dt: datetime) -> str:
    local_dt = ensure_local(dt)
    assert local_dt is not None
    return local_dt.strftime(ISO_FORMAT)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, ISO_FORMAT)
    except ValueError:
        try:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
        except Exception:
            return None
    return ensure_local(dt)

def parse_time_string(time_str):
    match = TIME_PATTERN.fullmatch(time_str.strip().lower())
    if not match:
        return None
    days = int(match.group('days') or 0)
    hours = int(match.group('hours') or 0)
    minutes = int(match.group('minutes') or 0)
    total_seconds = days * 86400 + hours * 3600 + minutes * 60
    if total_seconds == 0:
        return None
    return timedelta(seconds=total_seconds)

def load_reminders():
    if not os.path.exists(REMINDER_FILE):
        return []
    try:
        with open(REMINDER_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('reminders', [])
    except Exception:
        return []

def save_reminders(reminders):
    os.makedirs(os.path.dirname(REMINDER_FILE), exist_ok=True)
    with open(REMINDER_FILE, 'w', encoding='utf-8') as f:
        json.dump({'reminders': reminders}, f, indent=2)

def remove_reminder(reminders, reminder_id: int):
    return [r for r in reminders if r.get('id') != reminder_id]

def purge_user_reminders(reminders, user_id):
    return [r for r in reminders if r['user_id'] != user_id]


def humanize_timedelta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)

class RemindersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reminders = load_reminders()
        # Migrate any legacy non-numeric IDs and timestamps to the preferred format
        migrated_ids = self._normalize_ids()
        migrated_ts = self._normalize_timestamps()
        if migrated_ids or migrated_ts:
            save_reminders(self.reminders)
        self.check_reminders.start()
        # Inform once if persona ToolBridge isn't available
        if ToolBridge is None:
            logging.getLogger(__name__).warning("ToolBridge not available; persona-styled reminder messages will be skipped (using plain text). Ensure PYTHONPATH includes 'src'.")

    def _next_available_id(self) -> int:
        used: set[int] = set()
        for r in self.reminders:
            try:
                if isinstance(r.get('id'), int):
                    used.add(r['id'])
                else:
                    # Consider numeric strings as used as well
                    v = r.get('id')
                    if isinstance(v, str) and v.isdigit():
                        used.add(int(v))
            except Exception:
                continue
        i = 1
        while i in used:
            i += 1
        return i

    def _normalize_ids(self) -> bool:
        changed = False
        for r in self.reminders:
            if not isinstance(r.get('id'), int):
                try:
                    # If it's a numeric string, convert in place
                    v = r.get('id')
                    if isinstance(v, str) and v.isdigit():
                        r['id'] = int(v)
                        changed = True
                        continue
                except Exception:
                    pass
                # Assign a new smallest available integer ID
                r['id'] = self._next_available_id()
                changed = True
        return changed

    def _normalize_timestamps(self) -> bool:
        changed = False
        for r in self.reminders:
            for key in ("created_at", "remind_at", "next_retry"):
                val = r.get(key)
                if not isinstance(val, str):
                    continue
                dt = parse_timestamp(val)
                if dt is None:
                    continue
                formatted = format_timestamp(dt)
                if formatted != val:
                    r[key] = formatted
                    changed = True
        return changed

    async def cleanup_stale_reminders(self):
        now = now_local()
        stale = []
        for r in self.reminders:
            ra = parse_timestamp(r.get('remind_at'))
            if ra is None:
                continue
            if ra < now:
                stale.append(r)
        for reminder in stale:
            await self.deliver_reminder(reminder, offline=True)
            self.reminders = remove_reminder(self.reminders, reminder['id'])
        if stale:
            save_reminders(self.reminders)

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def check_reminders(self):
        now = now_local()
        updated_reminders = []
        for r in self.reminders:
            remind_at = parse_timestamp(r.get('remind_at'))
            if remind_at is None:
                updated_reminders.append(r)
                continue
            failed_attempts = r.get('failed_attempts', 0)
            if r.get('next_retry'):
                next_retry = parse_timestamp(r.get('next_retry')) or remind_at
            else:
                next_retry = remind_at
            # If reminder is due and not yet delivered
            if remind_at <= now:
                # If never failed, or retry time has passed
                if failed_attempts == 0 or next_retry <= now:
                    delivered = await self.deliver_reminder(r)
                    if delivered:
                        continue  # Remove from reminders
                    else:
                        # Schedule next retry in 10 minutes, up to 6 times (1 hour)
                        failed_attempts += 1
                        if failed_attempts < 7:
                            r['failed_attempts'] = failed_attempts
                            r['next_retry'] = format_timestamp(now + timedelta(minutes=10))
                            updated_reminders.append(r)
                        # After 6 failed attempts, give up and remove
                else:
                    updated_reminders.append(r)
            else:
                updated_reminders.append(r)
        self.reminders = updated_reminders
        save_reminders(self.reminders)

    async def deliver_reminder(self, reminder, offline=False):
        user = self.bot.get_user(reminder['user_id'])
        channel = self.bot.get_channel(reminder['channel_id'])
        # Build persona reply via ToolBridge
        router = getattr(self.bot, 'router', None)
        base_text = f"<@{reminder['user_id']}> 🔔 Reminder: {reminder['message']}"
        if router is not None and ToolBridge is not None:
            tool = ToolBridge(router)
            try:
                # Local time presentation for when it was scheduled
                ra = parse_timestamp(reminder.get('remind_at'))
                if ra is not None:
                    scheduled_local = ensure_local(ra).strftime('%Y-%m-%d %H:%M')
                else:
                    scheduled_local = reminder.get('remind_at', '')
                details = f"scheduled_at_local={scheduled_local} delivered_late={'true' if offline else 'false'}"
                is_nsfw = bool(getattr(getattr(channel, 'parent', None), 'nsfw', False)) or bool(getattr(channel, 'nsfw', False)) if channel else False
                summary = base_text
                reply = await tool.run(
                    channel_id=str(getattr(channel, 'id', 'dm')),
                    tool_name="reminder",
                    intent="deliver_reminder",
                    summary=summary,
                    details=details,
                    block_char_limit=1024,
                    is_nsfw=is_nsfw,
                    temperature=0.35,
                    style_hint="You are a personal assistant, you may make some commentary about delivering the reminder but do not refer to the reminder content. Then Deliver the reminder verbatim, include the user mention exactly as provided. If delivered late, briefly acknowledge it. Do not ask questions.",
                )
            except Exception as e:
                logging.getLogger(__name__).debug(f"ToolBridge error in deliver_reminder: {e}")
                reply = None
        else:
            reply = None

        text = reply or (base_text + ("\n(Note: The bot was offline when your reminder was originally scheduled.)" if offline else ""))
        delivered = False
        # Try channel first
        if channel:
            try:
                splitter = getattr(router, '_split_for_discord', None) if router else None
                if callable(splitter):
                    parts = splitter(text)
                    if not isinstance(parts, (list, tuple)):
                        parts = [str(parts)]
                else:
                    parts = [text]
                for part in parts:
                    await channel.send(part)
                delivered = True
            except Exception:
                delivered = False
        # Fallback to DM
        if not delivered and user:
            try:
                await user.send(text)
                delivered = True
            except Exception:
                delivered = False
        if not delivered:
            # User left or banned, purge their reminders
            self.reminders = purge_user_reminders(self.reminders, reminder['user_id'])
            save_reminders(self.reminders)
        return delivered

    @app_commands.command(name="remind", description="Set a reminder. Usage: /remind 1d2h30m Check Nyaa")
    async def remind(self, interaction: discord.Interaction, time: str, message: str):
        await interaction.response.defer(ephemeral=False)
        delta = parse_time_string(time)
        if not delta:
            await interaction.followup.send("Invalid time format. Use e.g. 1d2h30m, 45m, 2h.", ephemeral=True)
            return
        remind_at = now_local() + delta
        created_at = now_local()
        reminder = {
            'id': self._next_available_id(),
            'user_id': interaction.user.id,
            'channel_id': interaction.channel.id if interaction.channel else None,
            'guild_id': interaction.guild.id if interaction.guild else None,
            'message': message,
            'created_at': format_timestamp(created_at),
            'remind_at': format_timestamp(remind_at)
        }
        self.reminders.append(reminder)
        save_reminders(self.reminders)
        # Persona confirmation via ToolBridge
        router = getattr(self.bot, 'router', None)
        if router is not None and ToolBridge is not None:
            tool = ToolBridge(router)
            human = humanize_timedelta(delta)
            try:
                local_time = ensure_local(remind_at).strftime('%Y-%m-%d %H:%M')
            except Exception:
                local_time = reminder['remind_at']
            summary = f"Set a reminder for <@{interaction.user.id}> in {human}: {message}"
            details = f"remind_at_local={local_time}"
            try:
                is_nsfw = bool(getattr(getattr(interaction, 'channel', None), 'nsfw', False)) or bool(getattr(getattr(getattr(interaction, 'channel', None), 'parent', None), 'nsfw', False))
                reply = await tool.run(
                    channel_id=str(getattr(getattr(interaction, 'channel', None), 'id', 'web-room')),
                    tool_name="reminder",
                    intent="create_reminder",
                    summary=summary,
                    details=details,
                    block_char_limit=1024,
                    is_nsfw=is_nsfw,
                    temperature=0.35,
                    style_hint="You are a personal assistant, Respond as if you are Acknowledging and set the reminder by repeating with the human-friendly delay and reminder. Be concise and warm. Do not ask questions.",
                )
            except Exception as e:
                logging.getLogger(__name__).debug(f"ToolBridge error in remind: {e}")
                reply = None
            text = reply or f"Reminder set for <@{interaction.user.id}> in {time}: {message}"
            try:
                splitter = getattr(router, '_split_for_discord', None)
                if callable(splitter):
                    parts = splitter(text)
                    if not isinstance(parts, (list, tuple)):
                        parts = [str(parts)]
                else:
                    parts = [text]
                for part in parts:
                    await interaction.followup.send(part, ephemeral=False)
            except Exception:
                await interaction.followup.send(text, ephemeral=False)
        else:
            await interaction.followup.send(f"Reminder set for <@{interaction.user.id}> in {time}: {message}", ephemeral=False)

    @app_commands.command(name="reminders", description="List your active reminders with their IDs.")
    async def reminders_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_reminders = [r for r in self.reminders if r['user_id'] == interaction.user.id]
        if not user_reminders:
            await interaction.followup.send("You have no active reminders.", ephemeral=True)
            return
        lines = []
        for r in user_reminders:
            at = parse_timestamp(r.get('remind_at'))
            if at is not None:
                at_local = ensure_local(at).strftime('%Y-%m-%d %H:%M')
            else:
                at_local = r.get('remind_at', '')
            lines.append(f"ID: {r['id']} | At: {at_local} | Msg: {r['message']}")
        summary = "Your active reminders:"
        details = "\n".join(lines)
        router = getattr(self.bot, 'router', None)
        if router is not None and ToolBridge is not None:
            tool = ToolBridge(router)
            try:
                reply = await tool.run(
                    channel_id=str(getattr(getattr(interaction, 'channel', None), 'id', 'web-room')),
                    tool_name="reminder",
                    intent="list_reminders",
                    summary=summary,
                    details=details,
                    block_char_limit=2048,
                    is_nsfw=False,
                    temperature=0.35,
                    style_hint="You are a personal assistant, Respond as if you are Acknowledging and Return a count of the amount of reminders and then present the list of reminders inclusive of ID's neatly. Do not ask questions.",
                )
            except Exception as e:
                logging.getLogger(__name__).debug(f"ToolBridge error in reminders_list: {e}")
                reply = None
            await interaction.followup.send(reply or (summary + "\n" + details), ephemeral=True)
        else:
            await interaction.followup.send(summary + "\n" + details, ephemeral=True)

    @app_commands.command(name="remindercancel", description="Cancel a specific reminder by ID.")
    async def cancel_reminder(self, interaction: discord.Interaction, reminder_id: int):
        await interaction.response.defer(ephemeral=True)
        before = len(self.reminders)
        self.reminders = remove_reminder(self.reminders, int(reminder_id))
        after = len(self.reminders)
        save_reminders(self.reminders)
        router = getattr(self.bot, 'router', None)
        if before == after:
            text = f"No reminder found with ID {reminder_id}."
        else:
            text = f"Reminder {reminder_id} cancelled."
        if router is not None and ToolBridge is not None:
            tool = ToolBridge(router)
            try:
                reply = await tool.run(
                    channel_id=str(getattr(getattr(interaction, 'channel', None), 'id', 'web-room')),
                    tool_name="reminder",
                    intent="cancel_reminder",
                    summary=text,
                    details=None,
                    block_char_limit=512,
                    is_nsfw=False,
                    temperature=0.35,
                    style_hint="You are a personal assistant, Respond as if you are Acknowledging the cancellation (or missing ID) concisely by referring to the ID or reminder. Do not ask questions.",
                )
            except Exception as e:
                logging.getLogger(__name__).debug(f"ToolBridge error in cancel_reminder: {e}")
                reply = None
            await interaction.followup.send(reply or text, ephemeral=True)
        else:
            await interaction.followup.send(text, ephemeral=True)

async def setup(bot):
    cog = RemindersCog(bot)
    await cog.cleanup_stale_reminders()
    await bot.add_cog(cog)
