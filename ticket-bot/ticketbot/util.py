"""Small shared helpers: JSON columns, formatting, permission checks."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import discord

BLURPLE = 0x5865F2
GREEN = 0x57F287
RED = 0xED4245
GREY = 0x95A5A6
DARK = 0x2B2D31          # Discord's dark-theme background -> embeds look borderless

QUICK_PREFIX = "quick-"  # quick panels are stored as 'quick-<channel id>'

CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def jload(value, default=None):
    """Load a JSON list/dict column, tolerating NULL / garbage."""
    if default is None:
        default = []
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def is_quick(panel: dict) -> bool:
    return panel["name"].startswith(QUICK_PREFIX)


# Pages that Discord turns into an animated GIF only when the link is posted as plain message text.
GIF_PAGE_HOSTS = {"tenor.com", "www.tenor.com", "giphy.com", "www.giphy.com", "gph.is"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def classify_link(url: str) -> str:
    """'link' for Tenor/Giphy pages (posted as message text so Discord unfurls the GIF), else 'image'."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in GIF_PAGE_HOSTS and not parsed.path.lower().endswith(IMAGE_EXTS):
        return "link"
    return "image"


def is_expiring_discord_link(url: str) -> bool:
    """Links to files uploaded in Discord stop working after a while."""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() in ("cdn.discordapp.com", "media.discordapp.net") \
        and "/attachments/" in parsed.path


def valid_emoji(text: str) -> bool:
    return bool(CUSTOM_EMOJI_RE.match(text)) or (len(text) <= 12 and not text.isascii())


def truncate(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(text: str, **values) -> str:
    """Replace {placeholders} without ever raising on unknown braces."""
    for key, val in values.items():
        text = text.replace("{" + key + "}", str(val))
    return text


def channel_name(fmt: str, *, number: int, username: str, category: str) -> str:
    name = render(fmt, number=f"{number:04d}", username=username, category=category)
    name = re.sub(r"[^a-z0-9_\-]+", "-", name.lower()).strip("-")[:90]
    return name or f"ticket-{number:04d}"


def ensure_number(name: str, number: int) -> str:
    """Guarantee the original ticket number survives any renaming."""
    tag = f"{number:04d}"
    return name if tag in name else f"{name[:90]}-{tag}"


async def get_settings(bot, guild_id: int) -> dict:
    row = await bot.db.get("guild_settings", guild_id)
    if row is None:
        await bot.db.execute("INSERT OR IGNORE INTO guild_settings (id) VALUES (?)", (guild_id,))
        row = await bot.db.get("guild_settings", guild_id)
    return row


def is_admin(member: discord.Member, settings: dict) -> bool:
    if member.guild_permissions.administrator or member.id == member.guild.owner_id:
        return True
    ids = set(jload(settings["admin_role_ids"]))
    return any(r.id in ids for r in member.roles)


def is_staff(member, settings: dict, category: dict | None = None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if is_admin(member, settings):
        return True
    ids = set(jload(settings["staff_role_ids"]))
    if category:
        ids |= set(jload(category["staff_role_ids"]))
    return any(r.id in ids for r in member.roles)


# ---------------------------------------------------------------------------
# Decorative symbols (used instead of emoji everywhere the bot talks)
# ---------------------------------------------------------------------------
OK = "ʚ♡ɞ"
ERR = "⋆｡°✩"


def decorate(label: str, emoji: str | None) -> tuple[str, str | None]:
    """Buttons/dropdown options: a real custom emoji stays an emoji, anything else
    (e.g. ♡ ʚɞ ୨୧) is written in front of the label, because Discord only accepts real emoji there."""
    if not emoji:
        return label, None
    if CUSTOM_EMOJI_RE.match(emoji):
        return label, emoji
    return truncate(f"{emoji} {label}", 100), None
