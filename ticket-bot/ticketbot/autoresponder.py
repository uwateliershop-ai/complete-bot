"""Auto-responders: keyword replies with text, an embed card and buttons.

Everything is configured in /dashboard → Auto-Responders. A responder can live in ticket channels only
(default) or in the whole server / one channel, and its reply can carry buttons (link, reply, role, open ticket).
This module also keeps the per-ticket activity timestamps that the idle / unclaimed timers rely on.
"""
from __future__ import annotations

import logging
import re
import time

import discord

from . import actions, media
from .util import get_settings, is_staff, render, truncate

log = logging.getLogger(__name__)

_seen: dict[tuple[int, int], float] = {}       # (channel id, responder id) -> monotonic time of the last reply


def now_iso() -> str:
    return discord.utils.utcnow().isoformat()


def matches(row: dict, text: str) -> bool:
    t = " ".join(text.lower().split())
    keys = [k.strip().lower() for k in (row["trigger"] or "").split(",") if k.strip()]
    if not keys:
        return False
    mode = row["match_type"]
    if mode == "exact":
        return t in keys
    if mode == "starts":
        return any(t.startswith(k) for k in keys)
    return any(re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", t) for k in keys)


# ---------------------------------------------------------------------------
# buttons on an auto-reply message
# ---------------------------------------------------------------------------
class ResponderButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tb:ar:(?P<id>[0-9]+)"):
    """Persistent: survives restarts without re-registering every message."""

    def __init__(self, data: dict, row: int | None = None):
        super().__init__(discord.ui.Button(custom_id=f"tb:ar:{data['id']}", row=row, **actions.button_kwargs(data)))
        self.button_id = data["id"]

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls({"id": int(match["id"]), "label": item.label or "·", "emoji": None, "style": "secondary"})

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        fresh = await bot.db.get("autoresponder_buttons", self.button_id)     # always the current configuration
        if not fresh or not fresh["enabled"]:
            return await interaction.response.send_message("⋆｡°✩ This button is no longer available.", ephemeral=True)
        await actions.run(interaction, bot, fresh)


def build_view(buttons: list[dict], *, preview: bool = False) -> discord.ui.View | None:
    items = [b for b in buttons if b["enabled"] and not actions.button_problem(b)][:25]
    if not items:
        return None
    view = discord.ui.View(timeout=600 if preview else None)
    for i, data in enumerate(items):
        if actions.is_link(data):
            view.add_item(discord.ui.Button(url=data["url"], row=i // 5,
                                            **{k: v for k, v in actions.button_kwargs(data).items() if k != "style"}))
        elif preview:
            view.add_item(discord.ui.Button(disabled=True, row=i // 5, **actions.button_kwargs(data)))
        else:
            view.add_item(ResponderButton(data, i // 5))
    return view


async def render_reply(bot, row: dict, values: dict, *, preview: bool = False) -> dict:
    """kwargs (content / embeds / view) for the message an auto-responder sends."""
    from . import panels
    guild = media.guild_for(bot, row["guild_id"])
    text = media.rich_text(guild, render(row["response"] or "", **values)).text
    embeds = []
    if row["embed_id"]:
        tpl = await bot.db.get("embed_templates", row["embed_id"])
        if tpl:
            embeds.append(await panels.build_embed(bot, tpl))
    buttons = await bot.db.where("autoresponder_buttons", order="position, id", responder_id=row["id"])
    view = build_view(buttons, preview=preview)
    kwargs: dict = {"content": truncate(text, 2000) or None, "embeds": embeds}
    if view is not None:
        kwargs["view"] = view
    return kwargs


def message_values(message: discord.Message, ticket: dict | None) -> dict:
    guild = message.guild
    return {"user": message.author.mention, "username": message.author.display_name, "server": guild.name,
            "shop": guild.name, "channel": getattr(message.channel, "mention", ""),
            "ticket": f"{ticket['number']:04d}" if ticket else ""}


# ---------------------------------------------------------------------------
# incoming messages
# ---------------------------------------------------------------------------
async def handle_message(bot, message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return
    guild, db = message.guild, bot.db
    ticket = await db.fetchone("SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (message.channel.id,))
    settings = await get_settings(bot, guild.id)
    staff = False
    if ticket:                                  # activity bookkeeping for the nudge / idle timers
        category = await db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        staff = is_staff(message.author, settings, category)
        updates = {"last_activity": now_iso(), "idle_warned": 0}
        if staff and message.author.id != ticket["owner_id"]:
            updates["staff_replied"] = 1
        await db.update("tickets", ticket["id"], updates)
    else:
        staff = is_staff(message.author, settings)

    content = (message.content or "").strip()
    if not content or not settings["autoreply_enabled"]:
        return
    now = time.monotonic()
    for row in await db.where("autoresponders", order="id", guild_id=guild.id, enabled=1):
        if row["scope"] == "tickets":
            if not ticket or (ticket["claimed_by"] and not row["when_claimed"]):
                continue
        elif row["channel_id"] and row["channel_id"] != message.channel.id:
            continue
        if staff and not row["staff_triggers"]:
            continue
        if not matches(row, content):
            continue
        key = (message.channel.id, row["id"])
        if now - _seen.get(key, -1e9) < row["cooldown"]:
            continue
        kwargs = await render_reply(bot, row, message_values(message, ticket))
        if not kwargs["content"] and not kwargs["embeds"]:
            continue                            # nothing configured to say yet
        _seen[key] = now
        try:
            await message.channel.send(
                reference=message if row["reply"] else None, mention_author=False,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[message.author]),
                **kwargs)
        except discord.HTTPException:
            log.warning("Auto-responder %s could not post in %s", row["id"], message.channel.id)
        break                                   # one auto-reply per message
