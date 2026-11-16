"""Background timers for open tickets: nudge staff when nobody answered, remind a quiet owner, optionally auto-close.

All values and both messages are edited in /dashboard → Automation. 0 turns a timer off.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from . import media
from .util import BLURPLE, get_settings, jload, render

log = logging.getLogger(__name__)

DEFAULT_NUDGE = "Thanks for waiting {user}! A staff member will be with you shortly."
DEFAULT_IDLE = "Still there {user}? We haven't heard from you in a while."


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def staff_mentions(bot, guild: discord.Guild, ticket: dict, settings: dict) -> str:
    if ticket.get("claimed_by"):
        return f"<@{ticket['claimed_by']}>"
    category = await bot.db.get("categories", ticket["category_id"]) if ticket.get("category_id") else None
    ids = set(jload(settings["staff_role_ids"])) | set(jload(category["staff_role_ids"]) if category else [])
    return " ".join(r.mention for r in (guild.get_role(i) for i in ids) if r)


async def watch_once(bot) -> None:
    now = discord.utils.utcnow()
    for ticket in await bot.db.fetchall("SELECT * FROM tickets WHERE status = 'open'"):
        guild = bot.get_guild(ticket["guild_id"])
        channel = guild.get_channel(ticket["channel_id"]) if guild else None
        if channel is None:
            continue
        s = await get_settings(bot, guild.id)
        created = parse_iso(ticket["created_at"]) or now
        last = parse_iso(ticket["last_activity"]) or created
        values = {"user": f"<@{ticket['owner_id']}>", "number": f"{ticket['number']:04d}"}

        # nobody from staff has shown up yet
        mins = s["unclaimed_minutes"]
        if (mins and not ticket["nudged"] and not ticket["claimed_by"] and not ticket["staff_replied"]
                and now - created >= timedelta(minutes=mins)):
            await bot.db.update("tickets", ticket["id"], {"nudged": 1})
            ping = await staff_mentions(bot, guild, ticket, s)
            await channel.send(content=ping or None, embed=discord.Embed(
                description=media.rich_text(guild, render(s["nudge_message"] or DEFAULT_NUDGE, **values)).text, color=BLURPLE))
            continue

        # the ticket owner went quiet
        hours = s["idle_hours"]
        if not hours:
            continue
        if not ticket["idle_warned"] and now - last >= timedelta(hours=hours):
            await bot.db.update("tickets", ticket["id"], {"idle_warned": 1})
            text = media.rich_text(guild, render(s["idle_message"] or DEFAULT_IDLE, **values)).text
            if s["autoclose_hours"]:
                text += f"\nThis ticket will close automatically in {s['autoclose_hours']}h if there's no reply."
            await channel.send(content=values["user"], embed=discord.Embed(description=text, color=BLURPLE))
        elif (ticket["idle_warned"] and s["autoclose_hours"]
              and now - last >= timedelta(hours=hours + s["autoclose_hours"])):
            from .tickets import close_ticket
            await close_ticket(bot, channel, ticket["id"], guild.me)


async def watcher(bot) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await watch_once(bot)
        except Exception:
            log.exception("Ticket watcher failed")
        await asyncio.sleep(60)
