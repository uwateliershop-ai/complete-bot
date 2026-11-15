"""Panel rendering (embeds + buttons + dropdowns), publishing and live syncing."""
from __future__ import annotations

import asyncio
import json
import logging

import discord

from . import media, tickets
from .util import BLURPLE, DARK, decorate, is_quick, jload, truncate

log = logging.getLogger(__name__)

STYLES = {
    "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
}


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------
def _rich(guild, text):
    return media.rich_text(guild, text).text if text else text


async def build_embed(bot, tpl: dict) -> discord.Embed:
    guild = media.guild_for(bot, tpl.get("guild_id"))          # :server_emoji: becomes the real emoji
    embed = discord.Embed(title=_rich(guild, tpl["title"]) or None, description=_rich(guild, tpl["description"]) or None,
                          color=tpl["color"] if tpl["color"] is not None else BLURPLE)
    if tpl["show_timestamp"]:
        embed.timestamp = discord.utils.utcnow()
    if tpl["author_name"]:
        embed.set_author(name=tpl["author_name"], icon_url=tpl["author_icon"] or None)
    if tpl["footer_text"] or tpl["footer_icon"]:
        embed.set_footer(text=tpl["footer_text"] or "\u200b", icon_url=tpl["footer_icon"] or None)
    if tpl["thumbnail_url"]:
        embed.set_thumbnail(url=tpl["thumbnail_url"])
    if tpl["image_url"]:
        embed.set_image(url=tpl["image_url"])
    for f in await bot.db.where("embed_fields", order="position, id", embed_id=tpl["id"]):
        embed.add_field(name=_rich(guild, f["name"]), value=_rich(guild, f["value"]), inline=bool(f["is_inline"]))
    if not set(embed.to_dict()) - {"type", "color", "timestamp"}:   # Discord rejects empty embeds
        embed.description = "\u200b"
    return embed


# ---------------------------------------------------------------------------
# Persistent components
# ---------------------------------------------------------------------------
class PanelButton(discord.ui.Button):
    def __init__(self, bot, data: dict, row: int, preview: bool, disabled: bool):
        label, emoji = decorate(data["label"], data["emoji"])
        super().__init__(
            label=truncate(label, 80), style=STYLES.get(data["style"], discord.ButtonStyle.primary),
            emoji=emoji, row=row, disabled=preview or disabled,
            custom_id=f"tb:{'p' if preview else 'b'}:{data['id']}",
        )
        self.bot, self.data = bot, data

    async def callback(self, interaction: discord.Interaction):
        fresh = await self.bot.db.get("panel_buttons", self.data["id"])   # always use current config
        if not fresh or not fresh["enabled"]:
            return await interaction.response.send_message("⋆｡°✩ This button is no longer available.", ephemeral=True)
        await tickets.handle_entry(interaction, self.bot, category_id=fresh["category_id"], action=fresh["action"],
                                   form_id=fresh["form_id"], panel_id=fresh["panel_id"])


class PanelSelect(discord.ui.Select):
    def __init__(self, bot, data: dict, options: list[dict], row: int, preview: bool, disabled: bool):
        opts = options[:25]
        max_v = max(1, min(data["max_values"], len(opts)))
        super().__init__(
            placeholder=truncate(data["placeholder"], 150),
            min_values=min(data["min_values"], max_v),
            max_values=max_v,
            options=[discord.SelectOption(label=decorate(o["label"], o["emoji"])[0], value=f"opt:{o['id']}",
                                          description=o["description"] or None, emoji=decorate(o["label"], o["emoji"])[1])
                     for o in opts],
            row=row, disabled=preview or disabled,
            custom_id=f"tb:{'q' if preview else 's'}:{data['id']}",
        )
        self.bot, self.data = bot, data

    async def callback(self, interaction: discord.Interaction):
        option = await self.bot.db.get("dropdown_options", int(self.values[0].split(":")[1]))
        if option and option["enabled"]:
            panel_id = self.data["panel_id"]
            # NOTE: with max_values > 1 only the first chosen option opens a ticket.
            await tickets.handle_entry(interaction, self.bot, category_id=option["category_id"], action="create",
                                       form_id=None, panel_id=panel_id)
        else:
            await interaction.response.send_message("⋆｡°✩ This option is no longer available.", ephemeral=True)
        try:                                   # reset the dropdown so the same option can be picked again
            await interaction.message.edit(view=self.view)
        except discord.HTTPException:
            pass


class PanelView(discord.ui.View):
    def __init__(self, bot, buttons: list[dict], dropdowns: list[dict], options: dict[int, list[dict]],
                 *, preview: bool = False, disabled: bool = False):
        super().__init__(timeout=600 if preview else None)
        row = 0
        for dd in dropdowns:
            if options.get(dd["id"]):
                if row > 4:
                    raise ValueError("A panel can hold at most 5 component rows. Remove some dropdowns.")
                self.add_item(PanelSelect(bot, dd, options[dd["id"]], row, preview, disabled))
                row += 1
        for i, btn in enumerate(buttons):
            r = row + i // 5
            if r > 4:
                raise ValueError("A panel can hold at most 5 component rows (each dropdown uses a full row, "
                                 "each row fits 5 buttons). Remove some buttons or dropdowns.")
            self.add_item(PanelButton(bot, btn, r, preview, disabled))


async def render_panel(bot, panel: dict, *, preview: bool = False):
    """Return (embeds, view|None) for a panel. Raises ValueError for layouts Discord can't display."""
    db = bot.db
    embeds: list[discord.Embed] = []
    for eid in jload(panel["embed_ids"])[:10]:
        tpl = await db.get("embed_templates", eid)
        if tpl:
            embeds.append(await build_embed(bot, tpl))
    if not embeds and not panel.get("content") and not is_quick(panel):
        guild = media.guild_for(bot, panel.get("guild_id"))
        embed = discord.Embed(title=_rich(guild, panel["title"]) or panel["name"],
                              description=_rich(guild, panel["description"]) or None,
                              color=panel["color"] if panel["color"] is not None else BLURPLE)
        if panel["image_url"]:
            embed.set_image(url=panel["image_url"])
        if panel["thumbnail_url"]:
            embed.set_thumbnail(url=panel["thumbnail_url"])
        embeds.append(embed)
    buttons = [b for b in await db.where("panel_buttons", order="position, id", panel_id=panel["id"]) if b["enabled"]]
    dropdowns = [d for d in await db.where("panel_dropdowns", order="position, id", panel_id=panel["id"]) if d["enabled"]]
    options = {}
    for d in dropdowns:
        options[d["id"]] = [o for o in await db.where("dropdown_options", order="position, id", dropdown_id=d["id"])
                            if o["enabled"]]
    has_components = bool(buttons) or any(options.values())
    view = (PanelView(bot, buttons, dropdowns, options, preview=preview, disabled=not panel["enabled"])
            if has_components else None)
    return embeds, view


# ---------------------------------------------------------------------------
# Publishing / syncing
# ---------------------------------------------------------------------------
_locks: dict[int, asyncio.Lock] = {}       # one publish at a time per panel (several admins, many images)


def _message_kwargs(embeds, view, content=None, stickers=None) -> dict:
    kwargs = {"embeds": embeds}
    if not content and not embeds:
        content = media.ZWSP                                   # Discord rejects a message with only a dropdown
    if content:
        kwargs["content"] = content
        kwargs["allowed_mentions"] = discord.AllowedMentions.none()   # pasted text must never ping anyone
    if stickers:
        kwargs["stickers"] = stickers
    if view is not None:
        kwargs["view"] = view
    return kwargs


async def panel_stickers(bot, panel: dict, guild) -> tuple[list, list[str]]:
    """Stickers named as {sticker:Name} anywhere in the panel text or its embeds (Discord allows 3 per message)."""
    db = bot.db
    texts = [panel.get("content") or ""]
    for eid in jload(panel["embed_ids"])[:10]:
        tpl = await db.get("embed_templates", eid)
        if tpl:
            texts += [tpl["title"] or "", tpl["description"] or ""]
            for f in await db.where("embed_fields", order="position, id", embed_id=eid):
                texts += [f["name"], f["value"]]
    found, missing = [], []
    for text in texts:
        rich = media.rich_text(guild, text)
        found += [x for x in rich.stickers if x not in found]
        missing += [x for x in rich.missing if x not in missing]
    return found[:media.MAX_STICKERS], missing


async def delete_published(bot, panel: dict, *, decor: bool = True) -> None:
    """Delete the panel message and (unless decor=False) the picture messages posted above it."""
    channel = bot.get_channel(panel.get("message_channel_id") or 0)
    if channel is None:
        return
    ids = [panel.get("message_id")]
    if decor:
        ids += [panel.get("decor_message_id"), *jload(panel.get("image_message_ids"))]
    for message_id in ids:
        if message_id:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass


async def publish(bot, guild: discord.Guild, panel_id: int, channel: discord.abc.GuildChannel | None = None) -> str:
    """(Re)publish a panel as fresh messages: its pictures first, then the panel itself.
    The previous copy is removed only after the new one is up. Returns a human readable status line."""
    async with _locks.setdefault(panel_id, asyncio.Lock()):
        return await _publish(bot, guild, panel_id, channel)


async def _publish(bot, guild, panel_id: int, channel) -> str:
    db = bot.db
    panel = await db.get("panels", panel_id)
    if channel is not None:
        await db.update("panels", panel_id, {"channel_id": channel.id})
        panel["channel_id"] = channel.id
    target = guild.get_channel(panel["channel_id"] or 0)
    if not isinstance(target, discord.abc.Messageable):
        return "⋆｡°✩ Pick a destination channel for this panel first (Edit → Destination channel)."
    try:
        embeds, view = await render_panel(bot, panel)
    except ValueError as exc:
        return f"⋆｡°✩ {exc}"

    # 1. pictures (any number), posted above the panel
    images = await media.list_images(db, panel_id)
    thumbnail = panel.get("thumbnail_url") if is_quick(panel) else None
    try:
        gallery_ids, notes = await media.post_gallery(
            target, guild, images, thumbnail=thumbnail,
            color=DARK if is_quick(panel) or panel["color"] is None else panel["color"])
    except discord.HTTPException as exc:
        return f"⋆｡°✩ I couldn't post in {target.mention}: {truncate(getattr(exc, 'text', exc), 300)}"

    # 2. the panel message (text, embeds, dropdown / buttons, stickers)
    stickers, missing = await panel_stickers(bot, panel, guild)
    text = media.rich_text(guild, panel.get("content")).text or None
    try:
        try:
            message = await target.send(**_message_kwargs(embeds, view, text, stickers))
        except discord.HTTPException:
            if not stickers:
                raise
            stickers = []                                    # e.g. the bot may not use stickers here: post without
            notes.append("Stickers couldn't be sent in that channel, so the panel was posted without them.")
            message = await target.send(**_message_kwargs(embeds, view, text))
    except discord.HTTPException as exc:
        for message_id in gallery_ids:
            try:
                await target.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass
        return f"⋆｡°✩ Discord rejected the panel: {truncate(getattr(exc, 'text', exc), 300)}"

    await delete_published(bot, panel)                        # replace the previous copy (panel + pictures)
    await db.update("panels", panel_id, {
        "message_id": message.id, "message_channel_id": target.id,
        "decor_message_id": None, "image_message_ids": json.dumps(gallery_ids)})
    if view is not None:
        bot.add_view(view, message_id=message.id)
    notes += [f"⋆｡°✩ There is no sticker called **{name}** in this server." for name in missing]
    return "\n".join([f"ʚ♡ɞ Panel published in {target.mention}.", *notes])


async def affected_panels(bot, entity, row: dict) -> list[int]:
    """Which *published* panels need refreshing after `row` of `entity` changed."""
    db = bot.db
    name = entity.name
    if name == "panel":
        return [row["id"]]
    if name in ("button", "dropdown"):
        return [row["panel_id"]]
    if name == "option":
        dd = await db.get("panel_dropdowns", row["dropdown_id"])
        return [dd["panel_id"]] if dd else []
    if name in ("embed", "embed_field"):
        embed_id = row["id"] if name == "embed" else row["embed_id"]
        published = await db.fetchall("SELECT id, embed_ids FROM panels WHERE message_id IS NOT NULL")
        return [p["id"] for p in published if embed_id in jload(p["embed_ids"])]
    return []


async def sync(bot, panel_ids: list[int]) -> str | None:
    """Edit already-published panel messages in place. Returns a warning string on failure."""
    problems = []
    for pid in dict.fromkeys(panel_ids):
        panel = await bot.db.get("panels", pid)
        if not panel or not panel["message_id"]:
            continue
        channel = bot.get_channel(panel["message_channel_id"] or 0)
        if channel is None:
            continue
        try:
            embeds, view = await render_panel(bot, panel)
            text = media.rich_text(getattr(channel, "guild", None), panel.get("content")).text or None
            if not text and not embeds:
                text = media.ZWSP
            await channel.get_partial_message(panel["message_id"]).edit(content=text, embeds=embeds, view=view)
            if view is not None:
                bot.add_view(view, message_id=panel["message_id"])
        except discord.NotFound:
            await bot.db.update("panels", pid, {"message_id": None, "message_channel_id": None})
            problems.append(f"**{panel['name']}**: the published message was deleted – publish it again.")
        except (ValueError, discord.HTTPException) as exc:
            problems.append(f"**{panel['name']}**: {truncate(getattr(exc, 'text', exc), 200)}")
    return "⋆｡°✩ Saved, but a published panel couldn't be updated:\n" + "\n".join(problems) if problems else None


async def register_panel_views(bot) -> None:
    """Re-attach every published panel's components after a restart (persistent views)."""
    for panel in await bot.db.fetchall("SELECT * FROM panels WHERE message_id IS NOT NULL"):
        try:
            _, view = await render_panel(bot, panel)
            if view is not None:
                bot.add_view(view, message_id=panel["message_id"])
        except Exception:
            log.exception("Could not restore panel %s", panel["id"])
