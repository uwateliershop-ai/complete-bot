"""Server emojis, stickers, GIFs and the (unlimited) panel image gallery.

* ``:name:``        -> the server's custom emoji (animated ones too). Modals have no emoji picker, so this
                       shorthand is the way to use them everywhere text is typed.
* ``{sticker:Name}`` -> one of the server's stickers, sent along with the message (max 3 per message).
* Tenor / Giphy page links are posted as plain text so Discord shows the animated GIF.
* Panel images: any number, posted in order above the panel (10 per message, as Discord allows).
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import discord

from .util import DARK, URL_RE, classify_link

ZWSP = "\u200b"
SHORTCODE_RE = re.compile(r"(?<![\w<]):([A-Za-z0-9_]{2,32}):")
STICKER_RE = re.compile(r"\{sticker:([^{}\n]{1,60})\}", re.IGNORECASE)
LEADING_EMOJI_RE = re.compile(r"^\s*(<a?:\w{2,32}:\d{15,25}>)\s*(.*)$", re.DOTALL)
LEADING_SHORTCODE_RE = re.compile(r"^\s*(:[A-Za-z0-9_]{2,32}:)\s*(.*)$", re.DOTALL)
UPLOAD_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
MAX_STICKERS = 3


@dataclass
class Rich:
    text: str
    stickers: list = field(default_factory=list)
    missing: list = field(default_factory=list)


def guild_for(bot, guild_id):
    getter = getattr(bot, "get_guild", None)
    return getter(guild_id) if getter and guild_id else None


# ---------------------------------------------------------------------------
# Emojis + stickers inside text
# ---------------------------------------------------------------------------
def _emoji_lookup(guild) -> tuple[dict, dict]:
    emojis = [e for e in (getattr(guild, "emojis", None) or ()) if getattr(e, "available", True)]
    exact = {e.name: e for e in emojis}
    lower: dict = {}
    for e in emojis:
        lower.setdefault(e.name.lower(), e)
    return exact, lower


def emojify(guild, text: str | None) -> str | None:
    """Replace ``:name:`` with the server's custom emoji. Unknown names are left exactly as typed."""
    if not text or guild is None or ":" not in text:
        return text
    exact, lower = _emoji_lookup(guild)
    if not exact:
        return text

    def swap(match: re.Match) -> str:
        name = match.group(1)
        emoji = exact.get(name) or lower.get(name.lower())
        return str(emoji) if emoji else match.group(0)

    return SHORTCODE_RE.sub(swap, text)


def rich_text(guild, text: str | None) -> Rich:
    """Emojis resolved, ``{sticker:Name}`` tokens removed from the text and returned as sticker objects."""
    if not text:
        return Rich(text or "")
    found: list = []
    missing: list[str] = []
    lookup = {s.name.lower(): s for s in (getattr(guild, "stickers", None) or ())}

    def take(match: re.Match) -> str:
        name = match.group(1).strip()
        sticker = lookup.get(name.lower())
        if sticker is None:
            if name not in missing:
                missing.append(name)
        elif sticker not in found:
            found.append(sticker)
        return ""

    stripped = STICKER_RE.sub(take, text)
    if stripped != text:
        stripped = re.sub(r"[ \t]+\n", "\n", stripped).strip()
    return Rich(emojify(guild, stripped) or "", found[:MAX_STICKERS], missing)


def split_leading_emoji(guild, label: str) -> tuple[str | None, str]:
    """'<:rin:123> Order' or ':rin: Order' -> ('<:rin:123>', 'Order'). Used for button / option labels,
    where Discord only draws an emoji in the separate emoji slot."""
    label = label or ""
    match = LEADING_SHORTCODE_RE.match(label)
    if match:
        resolved = emojify(guild, match.group(1))
        if resolved != match.group(1) and match.group(2).strip():
            return resolved, match.group(2).strip()
        return None, label
    match = LEADING_EMOJI_RE.match(label)
    if match and match.group(2).strip():
        return match.group(1), match.group(2).strip()
    return None, label


# ---------------------------------------------------------------------------
# Image rows (table panel_images)
# ---------------------------------------------------------------------------
async def list_images(db, panel_id: int) -> list[dict]:
    return await db.where("panel_images", order="position, id", panel_id=panel_id)


async def _renumber(db, panel_id: int) -> list[dict]:
    rows = await list_images(db, panel_id)
    for pos, row in enumerate(rows):
        if row["position"] != pos:
            await db.update("panel_images", row["id"], {"position": pos})
            row["position"] = pos
    return rows


async def insert_images(db, panel_id: int, items: list[dict], position: int | None = None) -> int:
    """Add images (dicts with url / file_path / kind / label). `position` is 1-based; default = at the end."""
    rows = await _renumber(db, panel_id)
    idx = len(rows) if position is None else max(0, min(position - 1, len(rows)))
    for row in rows[idx:]:
        await db.update("panel_images", row["id"], {"position": row["position"] + len(items)})
    for offset, item in enumerate(items):
        await db.insert("panel_images", {
            "panel_id": panel_id, "url": item.get("url"), "file_path": item.get("file_path"),
            "label": item.get("label"), "kind": item.get("kind") or classify_link(item.get("url") or ""),
            "position": idx + offset})
    return idx + 1


async def remove_images(db, panel_id: int, first: int, last: int | None = None) -> list[dict]:
    """Delete images number first..last (1-based, inclusive). Returns the removed rows."""
    rows = await _renumber(db, panel_id)
    last = first if last is None else last
    gone = rows[first - 1:last]
    for row in gone:
        await db.delete("panel_images", row["id"])
    await _renumber(db, panel_id)
    return gone


async def purge_files(db, paths) -> None:
    """Delete uploaded files that no image row uses any more."""
    for path in {p for p in paths if p}:
        row = await db.fetchone("SELECT COUNT(*) AS n FROM panel_images WHERE file_path = ?", (path,))
        if row and row["n"] == 0:
            try:
                os.remove(path)
            except OSError:
                pass


def is_image_file(attachment) -> bool:
    kind = (getattr(attachment, "content_type", None) or "").lower()
    name = (getattr(attachment, "filename", "") or "").lower()
    return kind.startswith("image/") or name.endswith(UPLOAD_EXTS)


async def save_upload(attachment, guild_id: int) -> str:
    """Keep our own copy of an uploaded picture: Discord's attachment links expire."""
    folder = Path(os.getenv("IMAGE_DIR", "data/images")) / str(guild_id)
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(getattr(attachment, "filename", "") or "").suffix.lower()
    if ext not in UPLOAD_EXTS:
        ext = ".png"
    path = folder / f"{uuid.uuid4().hex}{ext}"
    await attachment.save(path)
    return str(path)


def parse_links(text: str) -> tuple[list[str], list[str]]:
    """Split pasted text (one link per line, spaces/commas also work) into (valid links, rejected pieces)."""
    good, bad = [], []
    for piece in re.split(r"[\s,]+", text or ""):
        if not piece:
            continue
        (good if URL_RE.match(piece) else bad).append(piece)
    return good, bad


def describe_image(row: dict) -> str:
    if row["kind"] == "file":
        return f"upload · {row.get('label') or Path(row['file_path'] or '').name}"
    url = row["url"] or ""
    if row["kind"] == "link":
        return f"GIF link · {url}"
    gif = url.lower().split("?")[0].endswith(".gif")
    return f"{'GIF' if gif else 'image'} · {url}"


# ---------------------------------------------------------------------------
# Posting the gallery
# ---------------------------------------------------------------------------
async def post_gallery(target, guild, images: list[dict], *, thumbnail: str | None = None,
                       color: int = DARK) -> tuple[list[int], list[str]]:
    """Post every image above a panel. Returns (message ids, notes). On a Discord error everything posted so far
    is deleted again and the error is re-raised."""
    posted: list[int] = []
    notes: list[str] = []
    embeds: list[discord.Embed] = []
    files: list[discord.File] = []
    size = 0
    budget = int((getattr(guild, "filesize_limit", None) or DEFAULT_UPLOAD_LIMIT) * 0.9)
    thumb_pending = thumbnail

    async def flush() -> None:
        nonlocal embeds, files, size
        if not embeds:
            return
        kwargs: dict = {"embeds": embeds}
        if files:
            kwargs["files"] = files
        message = await target.send(**kwargs)
        posted.append(message.id)
        embeds, files, size = [], [], 0

    try:
        for number, img in enumerate(images, start=1):
            if img["kind"] == "link":                                   # Tenor / Giphy page: let Discord unfurl it
                await flush()
                message = await target.send(content=img["url"], allowed_mentions=discord.AllowedMentions.none())
                posted.append(message.id)
                continue
            embed = discord.Embed(color=color)
            upload, weight = None, 0
            if img["kind"] == "file":
                path = Path(img["file_path"] or "")
                if not path.is_file():
                    notes.append(f"Picture {number} was skipped: its saved file is missing. Add it again.")
                    continue
                weight = path.stat().st_size
                name = f"image-{img['id']}{path.suffix}"
                embed.set_image(url=f"attachment://{name}")
                upload = discord.File(str(path), filename=name)
            else:
                embed.set_image(url=img["url"])
            if thumb_pending:
                embed.set_thumbnail(url=thumb_pending)
                thumb_pending = None
            if len(embeds) >= 10 or (upload and (len(files) >= 10 or (files and size + weight > budget))):
                await flush()
            embeds.append(embed)
            if upload:
                files.append(upload)
                size += weight
        if thumb_pending and not posted and not embeds:                # thumbnail but no pictures
            embeds.append(discord.Embed(description=ZWSP, color=color).set_thumbnail(url=thumb_pending))
        await flush()
    except discord.HTTPException:
        for message_id in posted:
            try:
                await target.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass
        raise
    return posted, notes
