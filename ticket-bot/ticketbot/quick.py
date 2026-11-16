"""One-step panel setup:  /quickpanel  (classic)  and  /boxpanel  (everything inside one box)

A single form builds the images, text, bottom bar and the dropdown of ticket categories, and posts them
in the same order as a classic ticket panel:

    [ picture ] [ picture ] ...  <- any number of pictures / GIFs, 10 per message (see /image)
    plain text lines             <- normal message text, no card around it (:emoji: and {sticker:Name} work)
    [ bottom bar ]               <- small embed with icon + decorative line
    [ Select a topic...  v ]     <- the dropdown, one option per ticket category

Running the command again in the same channel pre-fills the form with the current values,
so editing a panel is just "run it again, change what you want, submit".
"""
from __future__ import annotations

import json
from typing import Optional

import discord
from discord import app_commands

from . import media, panels
from .util import CUSTOM_EMOJI_RE, DARK, ERR, URL_RE, classify_link, get_settings, jload, truncate

PARTS = ("banner", "text", "bar")        # embed templates older versions created (cleaned up on re-run)
DEFAULT_PLACEHOLDER = "Select a topic..."
TEXT_LIMIT = 2000        # Discord limit for plain message text


def _key(channel_id: int) -> str:
    return f"quick-{channel_id}"


async def _existing(bot, guild_id: int, channel_id: int) -> dict:
    """The previous quick panel of this channel (used to pre-fill the form)."""
    db, key = bot.db, _key(channel_id)
    panel = await db.fetchone("SELECT * FROM panels WHERE guild_id = ? AND name = ?", (guild_id, key))
    if not panel:
        return {}
    out: dict = {"panel": panel}
    for part in PARTS:
        out[part] = await db.fetchone(
            "SELECT * FROM embed_templates WHERE guild_id = ? AND name = ?", (guild_id, f"{key}-{part}"))
    out["images"] = await media.list_images(db, panel["id"])
    dd = await db.fetchone("SELECT * FROM panel_dropdowns WHERE panel_id = ? ORDER BY position, id", (panel["id"],))
    out["dropdown"] = dd
    out["options"] = (await db.where("dropdown_options", order="position, id", dropdown_id=dd["id"])) if dd else []
    out["questions"] = {}
    for o in out["options"]:
        cat = await db.get("categories", o["category_id"]) if o["category_id"] else None
        if cat and cat["form_id"]:
            form = await db.get("forms", cat["form_id"])
            if form and form["name"] == cat["name"]:
                rows = await db.where("form_questions", order="position, id", form_id=form["id"])
                out["questions"][cat["id"]] = [(r["label"], r["style"] == "paragraph") for r in rows]
    return out


def parse_questions(text: str) -> list[tuple[str, bool]]:
    """'Server name ; Details+'  ->  [("Server name", False), ("Details", True)]   (+ = long answer)."""
    out = []
    for raw in (text or "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        long = raw.endswith("+")
        label = raw.rstrip("+").strip()[:45]          # Discord form inputs allow 45 characters
        if label:
            out.append((label, long))
    return out


def parse_topics(text: str, guild=None) -> tuple[list[dict], str | None]:
    """Each line:  Label | description | symbol | question ; question+ ; ...   (everything after Label is optional).
    The symbol (or an emoji at the start of the label) can be a server emoji: `:name:`."""
    topics, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 3)]
        lead, label = media.split_leading_emoji(guild, parts[0])
        label = label[:100]
        if not label:
            continue
        if label.lower() in seen:
            return [], f"The category **{label}** is listed twice."
        seen.add(label.lower())
        desc = parts[1][:100] if len(parts) > 1 and parts[1] else None
        emoji = media.emojify(guild, parts[2]).strip() if len(parts) > 2 and parts[2] else None
        if emoji and not CUSTOM_EMOJI_RE.match(emoji):
            emoji = emoji[:20]                           # decorative text symbols are written before the label
        emoji = emoji or lead
        questions = parse_questions(parts[3]) if len(parts) > 3 else []
        if len(questions) > 5:
            return [], f"**{label}** has {len(questions)} questions. Discord forms allow at most 5."
        topics.append({"label": label, "description": desc, "emoji": emoji, "questions": questions})
    if not topics:
        return [], "Add at least one ticket category (one per line)."
    if len(topics) > 25:
        return [], "A dropdown holds at most 25 categories."
    return topics, None


def _topics_text(options: list[dict], questions: dict | None = None) -> str | None:
    lines = []
    for o in options:
        qs = (questions or {}).get(o["category_id"], [])
        parts = [o["label"], o["description"] or "", o["emoji"] or "",
                 " ; ".join(label + ("+" if long else "") for label, long in qs)]
        while len(parts) > 1 and not parts[-1]:
            parts.pop()
        lines.append(" | ".join(parts))
    return "\n".join(lines) or None


async def _sync_form(db, guild_id: int, category_id: int, questions: list[tuple[str, bool]]) -> None:
    """Give a category its own form (named after the category) with these questions, or remove that form."""
    cat = await db.get("categories", category_id)
    own = await db.fetchone("SELECT * FROM forms WHERE guild_id = ? AND name = ?", (guild_id, cat["name"]))
    if questions:
        form_id = own["id"] if own else await db.insert("forms", {"guild_id": guild_id, "name": cat["name"]})
        await db.execute("DELETE FROM form_questions WHERE form_id = ?", (form_id,))
        for pos, (label, long) in enumerate(questions):
            await db.insert("form_questions", {
                "form_id": form_id, "label": label, "style": "paragraph" if long else "short",
                "required": 1, "position": pos})
        await db.update("categories", category_id, {"form_id": form_id})
    elif own and cat["form_id"] == own["id"]:
        await db.update("categories", category_id, {"form_id": None})
        await db.delete("forms", own["id"])            # only the form this command made (same name as the category)


class QuickPanelModal(discord.ui.Modal):
    def __init__(self, bot, channel: discord.TextChannel, *, images, thumbnail, staff_role, ticket_folder, old: dict,
                 layout: str = "classic"):
        super().__init__(title="Box panel" if layout == "box" else "Ticket panel")
        self.bot, self.channel, self.layout = bot, channel, layout
        self.images, self.thumbnail = list(images or []), thumbnail
        self.staff_role, self.ticket_folder = staff_role, ticket_folder
        bar_e = old.get("bar") or {}
        dd = old.get("dropdown") or {}
        old_text = (old.get("panel") or {}).get("content") or (old.get("text") or {}).get("description")
        self.f_text = discord.ui.TextInput(
            label="Panel text (:emoji: {sticker:Name} ok)", style=discord.TextStyle.paragraph, required=False,
            max_length=TEXT_LIMIT, default=(old_text or "")[:TEXT_LIMIT] or None,
            placeholder="· Read TOS in #rules\n· **OPEN** for tickets :server_emoji:")
        self.f_bar = discord.ui.TextInput(
            label="Bottom bar text (optional)", required=False, max_length=256,
            default=(bar_e.get("author_name") if bar_e.get("author_name") != "\u200b" else None) or None)
        self.f_icon = discord.ui.TextInput(
            label="Bottom bar icon link (optional)", required=False, max_length=500,
            default=bar_e.get("author_icon") or None, placeholder="https://...")
        self.f_placeholder = discord.ui.TextInput(
            label="Dropdown placeholder", required=True, max_length=150,
            default=dd.get("placeholder") or DEFAULT_PLACEHOLDER)
        self.f_topics = discord.ui.TextInput(
            label="Categories: Label|desc|symbol|questions", style=discord.TextStyle.paragraph, required=True,
            max_length=4000, default=_topics_text(old.get("options") or [], old.get("questions")),
            placeholder="Order | Buy | ♡ | What are you buying? ; Details+\nOther | Question | :server_emoji:")
        for item in (self.f_text, self.f_bar, self.f_icon, self.f_placeholder, self.f_topics):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        topics, error = parse_topics(self.f_topics.value, interaction.guild)
        text = (self.f_text.value or "").strip()
        bar_text = (self.f_bar.value or "").strip()
        bar_icon = (self.f_icon.value or "").strip() or None
        if not error and bar_icon and not URL_RE.match(bar_icon):
            error = "The bottom bar icon must be a link starting with http(s)://"
        if not error and not (self.images or self.thumbnail or text or bar_text or bar_icon):
            error = "The panel is empty. Add some text or an image."
        if error:
            return await interaction.response.send_message(
                f"⋆｡°✩ {error}\nRun `/{'boxpanel' if self.layout == 'box' else 'quickpanel'}` again to retry.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        bot, db, guild, channel = self.bot, self.bot.db, interaction.guild, self.channel
        key = _key(channel.id)

        # replace the previous version of this channel's quick panel (messages + settings)
        old = await _existing(bot, guild.id, channel.id)
        if old.get("panel"):
            await panels.delete_published(bot, old["panel"])           # panel message + banner message
            await db.delete("panels", old["panel"]["id"])              # cascades to dropdown + options
        for part in PARTS:
            await db.execute("DELETE FROM embed_templates WHERE guild_id = ? AND name = ?", (guild.id, f"{key}-{part}"))

        bar_id = (await db.insert("embed_templates", {
            "guild_id": guild.id, "name": f"{key}-bar", "color": DARK, "show_timestamp": 0,
            "author_name": bar_text or "\u200b", "author_icon": bar_icon})
                  if (bar_text or bar_icon) else None)

        panel_id = await db.insert("panels", {
            "guild_id": guild.id, "name": key, "color": None if self.layout == "box" else DARK,
            "layout": self.layout, "channel_id": channel.id,
            "thumbnail_url": self.thumbnail, "embed_ids": json.dumps([bar_id] if bar_id else []),
            "content": text or None})
        for pos, img in enumerate(self.images):              # pictures survive a re-run of the form
            await db.insert("panel_images", {
                "panel_id": panel_id, "url": img.get("url"), "file_path": img.get("file_path"),
                "label": img.get("label"), "kind": img["kind"], "position": pos})
        await media.purge_files(db, [i.get("file_path") for i in old.get("images") or []])   # replaced uploads
        dropdown_id = await db.insert("panel_dropdowns", {
            "panel_id": panel_id, "placeholder": truncate(self.f_placeholder.value.strip() or DEFAULT_PLACEHOLDER, 150),
            "min_values": 1, "max_values": 1, "position": 0})
        for pos, topic in enumerate(topics):
            row = await db.fetchone("SELECT id FROM categories WHERE guild_id = ? AND name = ?",
                                    (guild.id, topic["label"]))
            if row:
                category_id = row["id"]
                await db.update("categories", category_id, {"description": topic["description"]})
            else:
                category_id = await db.insert(
                    "categories", {"guild_id": guild.id, "name": topic["label"], "description": topic["description"]})
            await _sync_form(db, guild.id, category_id, topic["questions"])
            await db.insert("dropdown_options", {
                "dropdown_id": dropdown_id, "label": topic["label"], "description": topic["description"],
                "emoji": topic["emoji"], "category_id": category_id, "position": pos})

        settings = await get_settings(bot, guild.id)
        changes = {}
        if self.staff_role:
            ids = jload(settings["staff_role_ids"])
            if self.staff_role.id not in ids:
                changes["staff_role_ids"] = json.dumps(ids + [self.staff_role.id])
        if self.ticket_folder:
            changes["ticket_category_id"] = self.ticket_folder.id
        if changes:
            await db.update("guild_settings", guild.id, changes)
            settings = await get_settings(bot, guild.id)

        message = await panels.publish(bot, guild, panel_id, channel)
        cmd = "boxpanel" if self.layout == "box" else "quickpanel"
        notes = [message, f"To change anything later, run `/{cmd}` in the same channel again (the form is pre-filled), "
                          "or use `/dashboard` → Panels & Cards."]
        if not jload(settings["staff_role_ids"]):
            notes.append("⋆｡°✩ No staff role is set yet, so only admins can see tickets. "
                         "Run the command again and fill in the `staff_role` option.")
        await interaction.followup.send("\n".join(notes), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        import logging
        logging.getLogger(__name__).exception("quickpanel failed", exc_info=error)
        msg = "⋆｡°✩ Something went wrong while building the panel. Check the bot's console."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def add_quick_command(bot, group, admin_gate) -> None:
    for name, layout, description in (
            ("quickpanel", "classic", "Build and publish a complete ticket panel from one form."),
            ("boxpanel", "box", "Build a ticket panel where everything sits inside one box.")):
        _register(bot, group, admin_gate, name, layout, description)


def _register(bot, group, admin_gate, name: str, layout: str, description: str) -> None:
    @group.command(name=name, description=description)
    @app_commands.guild_only()
    @app_commands.describe(
        channel="Where the panel is posted",
        banner="Picture / GIF links, separated by spaces (optional). More later: /image add",
        thumbnail="Small image link shown on the right of the first picture (optional)",
        staff_role="Role that can see and manage tickets (optional)",
        ticket_folder="Discord category (folder) where new ticket channels are created (optional)")
    async def command(interaction: discord.Interaction, channel: discord.TextChannel,
                      banner: Optional[str] = None, thumbnail: Optional[str] = None,
                      staff_role: Optional[discord.Role] = None,
                      ticket_folder: Optional[discord.CategoryChannel] = None):
        if not await admin_gate(interaction, bot):
            return
        if thumbnail and not URL_RE.match(thumbnail):
            return await interaction.response.send_message(
                f"{ERR} The **thumbnail** must be a link starting with http(s)://", ephemeral=True)
        old = await _existing(bot, interaction.guild_id, channel.id)
        if banner:                                         # new links replace the old pictures
            links, bad = media.parse_links(banner)
            if bad or not links:
                return await interaction.response.send_message(
                    f"{ERR} The **banner** must be one or more links starting with http(s)://", ephemeral=True)
            images = [{"url": u, "kind": classify_link(u)} for u in links]
        else:
            images = old.get("images") or []
        thumbnail = thumbnail or (old.get("panel") or {}).get("thumbnail_url")
        await interaction.response.send_modal(QuickPanelModal(
            bot, channel, images=images, thumbnail=thumbnail,
            staff_role=staff_role, ticket_folder=ticket_folder, old=old, layout=layout))
