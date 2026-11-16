"""HTML transcript generation, archiving and retrieval."""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re
from pathlib import Path

import discord

from . import texts
from .util import BLURPLE, get_settings, jload

log = logging.getLogger(__name__)

INLINE_IMAGE_MAX = 1_500_000      # inline images up to ~1.5 MB as data: URIs
INLINE_BUDGET = 15_000_000        # total inline budget per transcript

CSS = """
body{margin:0;background:#313338;color:#dbdee1;font-family:'gg sans','Segoe UI',Helvetica,Arial,sans-serif;font-size:15px}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 48px}
.head{background:#2b2d31;border-radius:8px;padding:18px 22px;margin-bottom:20px;border-left:4px solid #5865f2}
.head h1{margin:0 0 10px;font-size:22px;color:#fff}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 20px;font-size:13px;color:#b5bac1}
.meta b{color:#f2f3f5}
.msg{display:flex;gap:14px;padding:6px 8px;border-radius:4px}.msg:hover{background:#2e3035}
.av{width:40px;height:40px;border-radius:50%;flex:none;background:#4e5058}
.name{font-weight:600;color:#fff}.tag{font-size:12px;color:#949ba4;margin-left:4px}
.bot{background:#5865f2;color:#fff;font-size:10px;border-radius:3px;padding:1px 5px;margin-left:6px;vertical-align:middle}
.ts{font-size:12px;color:#949ba4;margin-left:8px}
.content{white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;line-height:1.4}
.content a,.att a{color:#00a8fc}
code{background:#1e1f22;border-radius:3px;padding:1px 4px;font-size:13px}
pre{background:#1e1f22;border:1px solid #232428;border-radius:4px;padding:8px;overflow:auto;margin:4px 0}
pre code{padding:0;background:none}
img.emoji{height:20px;vertical-align:middle}
.att img{max-width:420px;max-height:320px;border-radius:6px;margin-top:6px;display:block}
.att a{display:inline-block;margin-top:6px;background:#2b2d31;padding:6px 10px;border-radius:4px}
.embed{border-left:4px solid #5865f2;background:#2b2d31;border-radius:4px;padding:8px 12px;margin-top:6px;max-width:520px}
.embed .t{font-weight:600;color:#fff}.embed .f{font-size:13px;margin-top:4px}
.sys{color:#949ba4;font-style:italic}
h2{font-size:15px;color:#fff;margin:28px 0 8px;border-bottom:1px solid #3f4147;padding-bottom:6px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:5px 8px;border-bottom:1px solid #3f4147;text-align:left}
th{color:#b5bac1}.foot{margin-top:28px;font-size:12px;color:#949ba4;text-align:center}
"""

LIGHT_CSS = """
body{background:#f2f3f5;color:#2e3338}.head,.embed,.att a{background:#fff}.head h1,.name,.embed .t,h2{color:#060607}
.meta{color:#4e5058}.meta b{color:#060607}.msg:hover{background:#e8eaed}.tag,.ts,.sys,.foot,th{color:#5c5e66}
code,pre{background:#e3e5e8;border-color:#d4d7dc}h2,td,th{border-bottom-color:#d4d7dc}.content a,.att a{color:#006ce7}
"""

LOG_LABELS = {
    "order_confirmed": "Order Confirmed", "order_processing": "Order Processing", "order_done": "Order Done",
    "ticket_created": "Ticket Created", "ticket_claimed": "Ticket Claimed", "ticket_unclaimed": "Ticket Unclaimed",
    "ticket_transferred": "Ticket Transferred", "user_added": "User Added", "user_removed": "User Removed",
    "ticket_renamed": "Ticket Renamed", "ticket_closed": "Ticket Closed", "ticket_reopened": "Ticket Reopened",
    "ticket_deleted": "Ticket Deleted",
}


def render_markdown(text: str) -> str:
    """Tiny, safe Discord-markdown → HTML converter (input is escaped first)."""
    text = html.escape(text, quote=False)
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```(?:\w+\n)?(.*?)```", lambda m: keep(f"<pre><code>{m.group(1)}</code></pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{m.group(1)}</code>"), text)
    text = re.sub(
        r"&lt;(a?):(\w+):(\d+)&gt;",
        lambda m: keep(
            f'<img class="emoji" alt=":{m.group(2)}:" '
            f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.{"gif" if m.group(1) else "png"}">'
        ),
        text,
    )
    text = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


async def _attachments_html(attachments, budget: list[int]) -> str:
    out = []
    for att in attachments:
        name = html.escape(att.filename)
        url = html.escape(att.url, quote=True)
        is_image = (att.content_type or "").startswith("image/")
        if is_image and att.size <= INLINE_IMAGE_MAX and budget[0] >= att.size:
            try:
                data = await att.read()
                budget[0] -= len(data)
                b64 = base64.b64encode(data).decode()
                out.append(f'<div class="att"><img alt="{name}" src="data:{att.content_type};base64,{b64}"></div>')
                continue
            except discord.HTTPException:
                pass
        if is_image:
            out.append(f'<div class="att"><a href="{url}" target="_blank"><img alt="{name}" src="{url}"></a></div>')
        else:
            out.append(f'<div class="att"><a href="{url}" target="_blank">˖ {name}</a></div>')
    return "".join(out)


def _embeds_html(embeds) -> str:
    out = []
    for e in embeds:
        color = f"#{e.color.value:06x}" if e.color else "#5865f2"
        parts = []
        if e.title:
            parts.append(f'<div class="t">{html.escape(e.title)}</div>')
        if e.description:
            parts.append(f'<div class="f">{render_markdown(e.description)}</div>')
        for fld in e.fields:
            parts.append(f'<div class="f"><b>{html.escape(fld.name)}</b><br>{render_markdown(fld.value)}</div>')
        if parts:
            out.append(f'<div class="embed" style="border-left-color:{color}">{"".join(parts)}</div>')
    return "".join(out)


async def render_html(bot, channel, ticket: dict, messages: list, logs: list, category, owner_label: str,
                      claimed_label: str, settings: dict | None = None) -> str:
    settings = settings or {}
    budget = [INLINE_BUDGET]
    rows = []
    for m in messages:
        author = m.author
        display = html.escape(getattr(author, "display_name", author.name))
        color = ""
        if isinstance(author, discord.Member) and author.color.value:
            color = f' style="color:#{author.color.value:06x}"'
        bot_tag = '<span class="bot">BOT</span>' if author.bot else ""
        stamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        if m.type not in (discord.MessageType.default, discord.MessageType.reply):
            body = f'<div class="content sys">{html.escape(m.system_content or "")}</div>'
        else:
            body = f'<div class="content">{render_markdown(m.clean_content)}</div>' if m.clean_content else ""
        body += _embeds_html(m.embeds) + await _attachments_html(m.attachments, budget)
        rows.append(
            f'<div class="msg"><img class="av" src="{html.escape(author.display_avatar.url)}" alt="">'
            f'<div><span class="name"{color}>{display}</span><span class="tag">@{html.escape(author.name)}</span>'
            f'{bot_tag}<span class="ts">{stamp}</span>{body}</div></div>'
        )
    log_rows = "".join(
        f"<tr><td>{html.escape(r['created_at'][:19].replace('T', ' '))}</td>"
        f"<td>{html.escape(LOG_LABELS.get(r['action'], r['action']))}</td>"
        f"<td>{html.escape(_name(channel.guild, r['actor_id']))}</td>"
        f"<td>{html.escape(r['details'] or '')}</td></tr>"
        for r in logs
    )
    answers = jload(ticket["form_answers"])
    form_html = ""
    if answers and settings.get("tr_show_form", 1):
        form_html = "<h2>Form answers</h2><table>" + "".join(
            f"<tr><th>{html.escape(a['q'])}</th><td>{html.escape(a['a'])}</td></tr>" for a in answers
        ) + "</table>"
    meta = [
        ("Ticket", f"#{ticket['number']:04d} (#{html.escape(channel.name)})"),
        ("Owner", html.escape(owner_label)),
        ("Assigned staff", html.escape(claimed_label)),
        ("Category", html.escape(category["name"]) if category else "—"),
        ("Created", html.escape(ticket["created_at"][:19].replace("T", " ") + " UTC")),
        ("Closed", html.escape((ticket["closed_at"] or "—")[:19].replace("T", " ")) + (" UTC" if ticket["closed_at"] else "")),
        ("Messages", str(len(messages))),
    ]
    meta_html = "".join(f"<div><b>{k}:</b> {v}</div>" for k, v in meta) if settings.get("tr_show_meta", 1) else ""
    now = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    guild = channel.guild
    values = {"number": f"{ticket['number']:04d}", "time": now, "server": guild.name,
              "category": category["name"] if category else "—"}
    title = render_markdown(texts.say(guild, settings, "tr_title", **values))
    footer = render_markdown(texts.say(guild, settings, "tr_footer", **values))
    accent = f"#{(settings.get('tr_color') if settings.get('tr_color') is not None else 0x5865F2):06x}"
    theme_css = LIGHT_CSS if (settings.get("tr_theme") or "dark").lower() == "light" else ""
    log_html = ""
    if settings.get("tr_show_log", 1):
        log_html = (f"<h2>Ticket activity</h2><table><tr><th>Time</th><th>Action</th><th>By</th><th>Details</th></tr>"
                    f"{log_rows}</table>")
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(re.sub(r'<[^>]+>', '', title))}</title><style>{CSS}{theme_css}"
        f".head{{border-left-color:{accent}}}</style></head><body><div class='wrap'>"
        f"<div class='head'><h1>{title}</h1><div class='meta'>{meta_html}</div></div>"
        f"{form_html}<h2>Conversation</h2>{''.join(rows) or '<div class=sys>No messages.</div>'}"
        f"{log_html}<div class='foot'>{footer}</div></div></body></html>"
    )


def _name(guild: discord.Guild, user_id: int | None) -> str:
    if not user_id:
        return "—"
    member = guild.get_member(user_id)
    return f"{member.display_name} ({user_id})" if member else str(user_id)


async def create_transcript(bot, channel: discord.TextChannel, ticket: dict, *, archive: bool = True) -> Path:
    """Build, save and (optionally) archive an HTML transcript. Returns the saved file path."""
    guild = channel.guild
    db = bot.db
    messages = [m async for m in channel.history(limit=None, oldest_first=True)]
    logs = await db.where("ticket_logs", ticket_id=ticket["id"])
    category = await db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
    document = await render_html(
        bot, channel, ticket, messages, logs, category,
        owner_label=_name(guild, ticket["owner_id"]),
        claimed_label=_name(guild, ticket["claimed_by"]),
        settings=await get_settings(bot, guild.id),
    )
    stamp = discord.utils.utcnow().strftime("%Y%m%d-%H%M%S")
    folder = Path(os.getenv("TRANSCRIPT_DIR", "data/transcripts")) / str(guild.id)
    path = folder / f"ticket-{ticket['number']:04d}-{stamp}.html"

    def write():
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")

    await asyncio.to_thread(write)

    record = {
        "ticket_id": ticket["id"], "guild_id": guild.id, "ticket_number": ticket["number"],
        "file_path": str(path), "created_at": discord.utils.utcnow().isoformat(),
    }
    record_id = await db.insert("transcripts", record)
    if archive:
        await send_to_archive(bot, guild, ticket, category, path, record_id, len(messages))
    return path


async def send_to_archive(bot, guild, ticket, category, path: Path, record_id: int, count: int) -> None:
    settings = await get_settings(bot, guild.id)
    channel = guild.get_channel(settings["archive_channel_id"] or 0)
    if not isinstance(channel, discord.abc.Messageable):
        return
    values = {"number": f"{ticket['number']:04d}", "server": guild.name, "category": category["name"] if category else "—",
              "user": f"<@{ticket['owner_id']}>"}
    accent = settings.get("tr_color") if settings.get("tr_color") is not None else BLURPLE
    embed = discord.Embed(title=texts.say(guild, settings, "tr_archive_title", **values)[:256], color=accent,
                          description=texts.say(guild, settings, "tr_archive_text", **values)[:4000] or None,
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Owner", value=f"<@{ticket['owner_id']}>")
    embed.add_field(name="Category", value=category["name"] if category else "—")
    embed.add_field(name="Assigned", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "—")
    embed.add_field(name="Messages", value=str(count))
    try:
        msg = await channel.send(embed=embed, file=discord.File(path))
    except discord.HTTPException as exc:
        log.warning("Could not post transcript to archive channel: %s", exc)
        return
    await bot.db.update("transcripts", record_id, {"archive_channel_id": channel.id, "archive_message_id": msg.id})


async def latest_transcript(bot, guild_id: int, number: int) -> dict | None:
    return await bot.db.fetchone(
        "SELECT * FROM transcripts WHERE guild_id = ? AND ticket_number = ? ORDER BY id DESC LIMIT 1",
        (guild_id, number),
    )
