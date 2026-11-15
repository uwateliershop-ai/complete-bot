"""Generic, entity-driven management screens used by the dashboard and the slash commands.

Every screen function returns a dict {content, embeds, view} that `ui.show()` renders.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3

import discord

from . import actions, autoresponder, media, panels
from .entities import ENTITIES, Entity, Field, Picker, display_value, parse_field
from .ui import BaseView, ConfirmView, show
from .util import BLURPLE, RED, get_settings, jload, truncate

log = logging.getLogger(__name__)
PAGE = 25

PANEL, EMBED = ENTITIES["panel"], ENTITIES["embed"]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def row_label(entity: Entity, row: dict) -> str:
    return str(row.get(entity.name_col) or f"#{row['id']}")


async def get_row(bot, entity: Entity, row_id: int, guild_id: int) -> dict | None:
    if entity.singleton:
        return await get_settings(bot, guild_id)
    return await bot.db.get(entity.table, row_id)


async def list_rows(bot, entity: Entity, guild_id: int, parent_id: int | None = None) -> list[dict]:
    order = "position, id" if entity.ordered else "id"
    if entity.parent_col:
        return await bot.db.where(entity.table, order=order, **{entity.parent_col: parent_id})
    return await bot.db.where(entity.table, order=order, guild_id=guild_id)


async def all_rows(bot, entity: Entity, guild_id: int) -> list[dict]:
    """Every row of an entity in the guild (children included)."""
    if entity.name == "dropdown":
        return await bot.db.fetchall(
            "SELECT d.* FROM panel_dropdowns d JOIN panels p ON p.id = d.panel_id "
            "WHERE p.guild_id = ? ORDER BY p.id, d.position, d.id", (guild_id,))
    return await list_rows(bot, entity, guild_id)


async def full_label(bot, entity: Entity, row: dict) -> str:
    label = row_label(entity, row)
    if entity.name == "dropdown":
        panel = await bot.db.get("panels", row["panel_id"])
        if panel:
            label = f"{panel['name']} › {label}"
    return label


async def after_change(bot, entity: Entity, row: dict | None) -> str | None:
    """Keep already-published panel messages in sync with configuration changes."""
    if row is None:
        return None
    return await panels.sync(bot, await panels.affected_panels(bot, entity, row))


async def delete_row(bot, entity: Entity, row: dict) -> str | None:
    affected = [] if entity.name == "panel" else await panels.affected_panels(bot, entity, row)
    files = []
    if entity.name == "panel":
        await panels.delete_published(bot, row)
        files = [i["file_path"] for i in await media.list_images(bot.db, row["id"])]
    await bot.db.delete(entity.table, row["id"])
    if files:
        await media.purge_files(bot.db, files)
    return await panels.sync(bot, affected) if affected else None


async def picker_display(bot, picker: Picker, value) -> str:
    if picker.kind == "roles":
        return " ".join(f"<@&{i}>" for i in jload(value)) or "—"
    if picker.kind == "role":
        return f"<@&{value}>" if value else "—"
    if picker.kind in ("channel", "category"):
        return f"<#{value}>" if value else "—"
    if picker.kind == "ref":
        ref = ENTITIES[picker.ref]
        row = await bot.db.get(ref.table, value) if value else None
        return row_label(ref, row) if row else "—"
    if picker.kind == "refs":
        ref, names = ENTITIES[picker.ref], []
        for i in jload(value):
            row = await bot.db.get(ref.table, i)
            if row:
                names.append(row_label(ref, row))
        return ", ".join(names) or "—"
    return "—"


async def describe(bot, entity: Entity, row: dict) -> discord.Embed:
    title = entity.title if entity.singleton else f"{entity.icon} {row_label(entity, row)}"
    embed = discord.Embed(title=truncate(title, 256), color=BLURPLE)
    if entity.singleton:
        embed.description = "Changes are saved automatically."
    for fields in entity.groups.values():
        for f in fields:
            embed.add_field(name=f.label.split(" (")[0].split(",")[0].split(":")[0],
                            value=truncate(display_value(f, row.get(f.key)) or "—", 200))
    for picker in entity.pickers:
        embed.add_field(name=picker.label, value=truncate(await picker_display(bot, picker, row.get(picker.key)), 300))
    toggles = _toggles(entity)
    if toggles:
        embed.add_field(name="Options", inline=False,
                        value="\n".join(f"{'ʚ♡ɞ' if row.get(k) else '⋆｡°✩'} {v}" for k, v in toggles.items()))
    if entity.name in ("button", "ar_button"):
        problem = actions.button_problem(row)
        if problem:
            embed.add_field(name="Needs attention", inline=False,
                            value=f"This button won't work until you {problem}.")
    if entity.name == "autoresponder":
        n = (await bot.db.fetchone("SELECT COUNT(*) AS n FROM autoresponder_buttons WHERE responder_id = ?",
                                   (row["id"],)))["n"]
        embed.add_field(name="Buttons", inline=False, value=f"{n} · manage with the **Buttons** button below")
    if entity.name == "panel":
        if row["message_id"] and row["message_channel_id"]:
            status = f"Published in <#{row['message_channel_id']}>"
        else:
            status = "Not published yet"
        embed.add_field(name="Status", value=status, inline=False)
        count = len(await media.list_images(bot.db, row["id"]))
        embed.add_field(name="Pictures", inline=False,
                        value=f"{count} · add with `/ticket image add`" if count else "None · add with `/ticket image add`")
    return embed


def _toggles(entity: Entity) -> dict[str, str]:
    toggles = {entity.enable_col: "Enabled"} if entity.enable_col else {}
    return {**toggles, **entity.toggles}


# ---------------------------------------------------------------------------
# Modal for text fields
# ---------------------------------------------------------------------------
class RowModal(discord.ui.Modal):
    def __init__(self, bot, entity: Entity, group: str, row: dict | None, parent_id: int | None, guild_id: int):
        action = "Edit" if row else "New"
        super().__init__(title=truncate(f"{action} {entity.singular}" + (f" – {group}" if row else ""), 45))
        self.bot, self.entity, self.group, self.row = bot, entity, group, row
        self.parent_id, self.guild_id = parent_id, guild_id
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for f in entity.groups[group]:
            value = row.get(f.key) if row else f.default
            self.inputs[f.key] = discord.ui.TextInput(
                label=truncate(f.label, 45), required=f.required, max_length=f.max,
                style=discord.TextStyle.paragraph if f.kind == "para" else discord.TextStyle.short,
                placeholder=f.placeholder or None, default=display_value(f, value) or None)
            self.add_item(self.inputs[f.key])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot, entity, db = self.bot, self.entity, self.bot.db
        data = {}
        guild = interaction.guild
        for f in entity.groups[self.group]:
            raw = self.inputs[f.key].value
            if f.kind == "emoji":
                raw = media.emojify(guild, raw)               # :server_emoji: -> <:server_emoji:123...>
            try:
                data[f.key] = parse_field(f, raw)
            except ValueError as exc:
                return await interaction.response.send_message(f"⋆｡°✩ **{f.label}**: {exc}", ephemeral=True)
        if entity.name in ("button", "ar_button", "option") and "label" in data and not data.get("emoji"):
            lead, label = media.split_leading_emoji(guild, data["label"] or "")
            if lead:                                          # "<emoji> Order" -> emoji slot + label "Order"
                data["emoji"], data["label"] = lead, label
        try:
            if self.row is None:
                if entity.scoped:
                    data["guild_id"] = self.guild_id
                if entity.parent_col:
                    data[entity.parent_col] = self.parent_id
                if entity.ordered:
                    pos = await db.fetchone(
                        f"SELECT COALESCE(MAX(position), -1) + 1 AS n FROM {entity.table} WHERE {entity.parent_col} = ?",
                        (self.parent_id,))
                    data["position"] = pos["n"]
                row_id = await db.insert(entity.table, data)
            else:
                row_id = self.row["id"]
                await db.update(entity.table, row_id, data)
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("⋆｡°✩ An item with that name already exists.", ephemeral=True)
        fresh = await get_row(bot, entity, row_id, self.guild_id)
        warn = await after_change(bot, entity, fresh)
        screen = await edit_screen(bot, interaction.user.id, self.guild_id, entity, row_id,
                                   notice=warn or "ʚ♡ɞ Saved.")
        await show(interaction, **screen)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal error", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("⋆｡°✩ Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
SECTIONS = [
    ("panels", "⋆", "Panels & Cards", "Post embed cards with buttons: tickets, links, replies, roles"),
    ("embeds", "⋆", "Embed Builder", "Customize embeds, images, fields and colors"),
    ("buttons", "⋆", "Button Management", "Ticket, link, reply and role buttons on a panel"),
    ("dropdowns", "⋆", "Dropdown Management", "Manage select menus and their options"),
    ("autoresponders", "⋆", "Auto-Responders", "Keyword replies with text, embed cards and buttons"),
    ("categories", "⋆", "Category Management", "Manage ticket categories and staff roles"),
    ("forms", "⋆", "Form Management", "Create and edit ticket forms"),
    ("automation", "⋆", "Automation", "Staff nudge, idle reminder, auto-close and their messages"),
    ("permissions", "⋆", "Permissions", "Manage staff roles and access permissions"),
    ("transcripts", "୨୧", "Transcript Settings", "Configure transcript generation and archives"),
    ("vouch", "♡", "Vouch Settings", "Vouch channel, request text and on/off"),
    ("general", "⋆", "General Settings", "Manage general ticket behavior"),
]


class DashboardView(BaseView):
    def __init__(self, bot, user_id: int):
        super().__init__(bot, user_id)
        select = discord.ui.Select(
            placeholder="Choose what to manage…",
            options=[discord.SelectOption(label=f"{icon} {name}", value=key, description=desc)
                     for key, icon, name, desc in SECTIONS])
        select.callback = self.chosen
        self.select = select
        self.add_item(select)

    async def chosen(self, interaction: discord.Interaction):
        screen = await section_screen(self.bot, interaction.user.id, interaction.guild_id, self.select.values[0])
        await show(interaction, **screen)


async def dashboard_screen(bot, user_id: int, guild_id: int) -> dict:
    embed = discord.Embed(title="୨୧ Ticket Bot Configuration", color=BLURPLE,
                          description="Manage your ticket system directly through Discord.\n\n" + "\n".join(
                              f"{icon} **{name}** – {desc}" for _, icon, name, desc in SECTIONS))
    embed.set_footer(text="Everything is saved automatically. No code editing required.")
    return {"embeds": [embed], "view": DashboardView(bot, user_id)}


async def section_screen(bot, user_id: int, guild_id: int, key: str) -> dict:
    E = ENTITIES
    if key == "panels":
        return await crud_screen(bot, user_id, guild_id, E["panel"])
    if key == "embeds":
        return await crud_screen(bot, user_id, guild_id, E["embed"])
    if key == "buttons":
        return await parent_pick_screen(bot, user_id, guild_id, E["button"])
    if key == "dropdowns":
        return await parent_pick_screen(bot, user_id, guild_id, E["dropdown"])
    if key == "autoresponders":
        return await crud_screen(bot, user_id, guild_id, E["autoresponder"])
    if key == "categories":
        return await crud_screen(bot, user_id, guild_id, E["category"])
    if key == "forms":
        return await crud_screen(bot, user_id, guild_id, E["form"])
    name = {"permissions": "permissions", "transcripts": "transcripts", "general": "general",
            "automation": "automation", "vouch": "vouch"}[key]
    return await edit_screen(bot, user_id, guild_id, E[name], guild_id)


# ---------------------------------------------------------------------------
# List / manage screen
# ---------------------------------------------------------------------------
EXTRAS = {
    "panel": [("Buttons", "⋆", "child:button"), ("Dropdowns", "⋆", "child:dropdown"),
              ("Preview", "⋆", "preview"), ("Publish / Republish", "⋆", "publish")],
    "embed": [("Fields", "⋆", "child:embed_field"), ("Preview", "⋆", "preview")],
    "dropdown": [("Options", "⋆", "child:option")],
    "form": [("Questions", "⋆", "child:question")],
    "autoresponder": [("Buttons", "⋆", "child:ar_button"), ("Preview", "⋆", "preview")],
}


async def crud_screen(bot, user_id: int, guild_id: int, entity: Entity, *, parent_id: int | None = None,
                      selected_id: int | None = None, page: int = 0, content: str | None = None) -> dict:
    rows = await list_rows(bot, entity, guild_id, parent_id)
    selected = next((r for r in rows if r["id"] == selected_id), None)
    pages = max(1, math.ceil(len(rows) / PAGE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE:(page + 1) * PAGE]

    lines = []
    for i, r in enumerate(chunk, start=page * PAGE + 1):
        mark = ""
        if entity.enable_col:
            mark = "ʚ♡ɞ " if r[entity.enable_col] else "⋆｡°✩ "
        pointer = "› " if selected and r["id"] == selected["id"] else ""
        lines.append(f"{pointer}`{i}.` {mark}{truncate(row_label(entity, r), 80)}")
    desc = "\n".join(lines) or f"*No {entity.title.lower()} yet. Press **Create** to add one.*"
    if entity.parent:
        parent = ENTITIES[entity.parent]
        prow = await bot.db.get(parent.table, parent_id)
        if prow:
            desc = f"Managing **{await full_label(bot, parent, prow)}**\n\n" + desc
    embed = discord.Embed(title=f"{entity.icon} {entity.title}", description=desc, color=BLURPLE)
    if entity.max_children:
        embed.set_footer(text=f"{len(rows)}/{entity.max_children} used • Page {page + 1}/{pages}")
    else:
        embed.set_footer(text=f"Page {page + 1}/{pages}")
    embeds = [embed]
    if selected:
        embeds.append(await describe(bot, entity, selected))
    view = CrudView(bot, user_id, entity, rows, chunk, parent_id, selected, page, pages)
    return {"content": content, "embeds": embeds, "view": view}


class CrudView(BaseView):
    def __init__(self, bot, user_id, entity, rows, chunk, parent_id, selected, page, pages):
        super().__init__(bot, user_id)
        self.entity, self.rows, self.parent_id = entity, rows, parent_id
        self.selected, self.page, self.pages = selected, page, pages
        has = selected is not None

        if chunk:
            select = discord.ui.Select(
                placeholder="Select an item to manage…", row=0,
                options=[discord.SelectOption(
                    label=truncate(f"{i}. {row_label(entity, r)}", 100), value=str(r["id"]),
                    default=has and r["id"] == selected["id"],
                    description=(("Enabled" if r[entity.enable_col] else "Disabled") if entity.enable_col else None))
                    for i, r in enumerate(chunk, start=page * PAGE + 1)])
            select.callback = self.on_select
            self.select = select
            self.add_item(select)

        self.btn("Create", self.on_create, style=discord.ButtonStyle.success, emoji="+", row=1)
        self.btn("Edit", self.on_edit, emoji="✎", row=1, disabled=not has)
        self.btn("Delete", self.on_delete, style=discord.ButtonStyle.danger, emoji="⋆", row=1, disabled=not has)
        if entity.enable_col:
            on = has and selected[entity.enable_col]
            self.btn("Disable" if on else "Enable", self.on_toggle, emoji="⟲", row=1, disabled=not has)
        if entity.ordered:
            self.btn("Up", lambda i: self.on_move(i, -1), emoji="⌃", row=2, disabled=not has)
            self.btn("Down", lambda i: self.on_move(i, 1), emoji="⌄", row=2, disabled=not has)
        if pages > 1:
            self.btn("Prev", lambda i: self.go(i, page=self.page - 1), emoji="‹", row=2, disabled=page == 0)
            self.btn("Next", lambda i: self.go(i, page=self.page + 1), emoji="›", row=2, disabled=page >= pages - 1)
        self.btn("Back", self.on_back, emoji="‹", row=2)
        for label, emoji, key in EXTRAS.get(entity.name, []):
            self.btn(label, self.make_extra(key), emoji=emoji, row=3, disabled=not has)

    async def go(self, interaction, *, selected_id=None, page=None, content=None, keep=True):
        sid = selected_id if selected_id is not None else (self.selected["id"] if self.selected and keep else None)
        screen = await crud_screen(self.bot, self.user_id, interaction.guild_id, self.entity, parent_id=self.parent_id,
                                   selected_id=sid, page=self.page if page is None else page, content=content)
        await show(interaction, **screen)

    async def on_select(self, interaction):
        await self.go(interaction, selected_id=int(self.select.values[0]))

    async def on_create(self, interaction):
        e = self.entity
        if e.max_children and len(self.rows) >= e.max_children:
            return await interaction.response.send_message(f"⋆｡°✩ Limit reached ({e.max_children}).", ephemeral=True)
        first = next(iter(e.groups))
        await interaction.response.send_modal(RowModal(self.bot, e, first, None, self.parent_id, interaction.guild_id))

    async def on_edit(self, interaction):
        screen = await edit_screen(self.bot, self.user_id, interaction.guild_id, self.entity, self.selected["id"])
        await show(interaction, **screen)

    async def on_delete(self, interaction):
        sel, e = self.selected, self.entity

        async def yes(i: discord.Interaction):
            warn = await delete_row(self.bot, e, sel)
            await self.go(i, content=warn or "⋆ Deleted.", keep=False)

        async def no(i: discord.Interaction):
            await self.go(i, selected_id=sel["id"])

        extra = " Everything inside it will be deleted too." if e.name in ("panel", "embed", "dropdown", "form", "autoresponder") else ""
        embed = discord.Embed(title="Delete?", color=RED,
                              description=f"Delete **{truncate(row_label(e, sel), 100)}**? This cannot be undone.{extra}")
        await show(interaction, embeds=[embed], view=ConfirmView(interaction.user.id, yes, no, yes_label="Delete"))

    async def on_toggle(self, interaction):
        e, sel = self.entity, self.selected
        await self.bot.db.update(e.table, sel["id"], {e.enable_col: 0 if sel[e.enable_col] else 1})
        warn = await after_change(self.bot, e, await self.bot.db.get(e.table, sel["id"]))
        await self.go(interaction, content=warn)

    async def on_move(self, interaction, delta: int):
        rows, sel, db, e = self.rows, self.selected, self.bot.db, self.entity
        idx = next(i for i, r in enumerate(rows) if r["id"] == sel["id"])
        j = idx + delta
        if 0 <= j < len(rows):
            rows[idx], rows[j] = rows[j], rows[idx]
            for pos, r in enumerate(rows):                     # normalise + apply the swap
                if r["position"] != pos:
                    await db.update(e.table, r["id"], {"position": pos})
            warn = await after_change(self.bot, e, await db.get(e.table, sel["id"]))
            return await self.go(interaction, page=j // PAGE, content=warn)
        await self.go(interaction)

    async def on_back(self, interaction):
        if self.entity.parent:
            screen = await parent_pick_screen(self.bot, self.user_id, interaction.guild_id, self.entity)
        else:
            screen = await dashboard_screen(self.bot, self.user_id, interaction.guild_id)
        await show(interaction, **screen)

    def make_extra(self, key: str):
        async def callback(interaction: discord.Interaction):
            sel, bot = self.selected, self.bot
            if key.startswith("child:"):
                screen = await crud_screen(bot, self.user_id, interaction.guild_id, ENTITIES[key[6:]], parent_id=sel["id"])
                return await show(interaction, **screen)
            if key == "preview" and self.entity.name == "autoresponder":
                guild, user = interaction.guild, interaction.user
                kwargs = await autoresponder.render_reply(bot, sel, {
                    "user": user.mention, "username": user.display_name, "server": guild.name, "shop": guild.name,
                    "channel": getattr(interaction.channel, "mention", ""), "ticket": "0001"}, preview=True)
                if not kwargs["content"] and not kwargs["embeds"]:
                    return await interaction.response.send_message(
                        "⋆｡°✩ Nothing to preview: add **Reply text** or pick an **embed card**.", ephemeral=True)
                kwargs["content"] = "⋆ **Preview** (not sent; buttons are disabled here)\n" + (kwargs["content"] or "")
                return await interaction.response.send_message(ephemeral=True, **kwargs)
            if key == "preview":
                try:
                    embeds, view = await preview_for(bot, self.entity, sel)
                except ValueError as exc:
                    return await interaction.response.send_message(f"⋆｡°✩ {exc}", ephemeral=True)
                kwargs = {"view": view} if view is not None else {}
                return await interaction.response.send_message("⋆ **Preview** (not published)", embeds=embeds,
                                                               ephemeral=True, **kwargs)
            if key == "publish":
                await interaction.response.defer()
                status = await panels.publish(bot, interaction.guild, sel["id"])
                await self.go(interaction, content=status)
        return callback


async def preview_for(bot, entity: Entity, row: dict):
    if entity.name == "panel":
        return await panels.render_panel(bot, row, preview=True)
    return [await panels.build_embed(bot, row)], None


# ---------------------------------------------------------------------------
# Parent chooser (for buttons / dropdowns / options / fields / questions)
# ---------------------------------------------------------------------------
async def parent_pick_screen(bot, user_id: int, guild_id: int, child: Entity) -> dict:
    parent = ENTITIES[child.parent]
    rows = (await all_rows(bot, parent, guild_id))[:25]
    embed = discord.Embed(title=f"{child.icon} {child.title}", color=BLURPLE)
    view = BaseView(bot, user_id)
    if not rows:
        embed.description = f"Create a **{parent.singular}** first – {child.title.lower()} belong to one."
    else:
        embed.description = f"Choose the **{parent.singular}** whose {child.title.lower()} you want to manage."
        options = [discord.SelectOption(label=truncate(await full_label(bot, parent, r), 100), value=str(r["id"]))
                   for r in rows]
        select = discord.ui.Select(placeholder=f"Choose a {parent.singular}…", options=options, row=0)

        async def chosen(interaction: discord.Interaction):
            screen = await crud_screen(bot, user_id, interaction.guild_id, child, parent_id=int(select.values[0]))
            await show(interaction, **screen)

        select.callback = chosen
        view.add_item(select)

    async def back(interaction: discord.Interaction):
        await show(interaction, **await dashboard_screen(bot, user_id, interaction.guild_id))

    view.btn("Dashboard", back, emoji="⌂", row=1)
    return {"embeds": [embed], "view": view}


# ---------------------------------------------------------------------------
# Edit screen (text groups, pickers, toggles)
# ---------------------------------------------------------------------------
async def edit_screen(bot, user_id: int, guild_id: int, entity: Entity, row_id: int, notice: str | None = None) -> dict:
    row = await get_row(bot, entity, row_id, guild_id)
    if row is None:
        return await dashboard_screen(bot, user_id, guild_id)
    return {"content": notice, "embeds": [await describe(bot, entity, row)],
            "view": EditMenu(bot, user_id, entity, row)}


class EditMenu(BaseView):
    def __init__(self, bot, user_id: int, entity: Entity, row: dict):
        super().__init__(bot, user_id)
        self.entity, self.row = entity, row
        for group in entity.groups:
            self.btn(group, self.make_group(group), emoji="✎", row=0)
        for picker in entity.pickers:
            self.btn(truncate(picker.label, 80), self.make_picker(picker), emoji="⋆", row=1)
        for col, label in _toggles(entity).items():
            on = bool(row.get(col))
            self.btn(f"{label}: {'ON' if on else 'OFF'}", self.make_toggle(col),
                     style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary, row=2)
        self.btn("Back", self.on_back, emoji="‹", row=3)

    def make_group(self, group: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(RowModal(
                self.bot, self.entity, group, self.row,
                self.row.get(self.entity.parent_col) if self.entity.parent_col else None, interaction.guild_id))
        return callback

    def make_toggle(self, col: str):
        async def callback(interaction: discord.Interaction):
            e = self.entity
            await self.bot.db.update(e.table, self.row["id"], {col: 0 if self.row.get(col) else 1})
            fresh = await get_row(self.bot, e, self.row["id"], interaction.guild_id)
            warn = await after_change(self.bot, e, fresh)
            await show(interaction, **await edit_screen(self.bot, self.user_id, interaction.guild_id, e,
                                                        self.row["id"], notice=warn))
        return callback

    def make_picker(self, picker: Picker):
        async def callback(interaction: discord.Interaction):
            await show(interaction, **await picker_screen(self.bot, self.user_id, interaction.guild_id,
                                                          self.entity, self.row, picker))
        return callback

    async def on_back(self, interaction: discord.Interaction):
        e = self.entity
        if e.singleton:
            screen = await dashboard_screen(self.bot, self.user_id, interaction.guild_id)
        else:
            screen = await crud_screen(self.bot, self.user_id, interaction.guild_id, e,
                                       parent_id=self.row.get(e.parent_col) if e.parent_col else None,
                                       selected_id=self.row["id"])
        await show(interaction, **screen)


# ---------------------------------------------------------------------------
# Pickers (role / channel / reference selects)
# ---------------------------------------------------------------------------
async def picker_screen(bot, user_id: int, guild_id: int, entity: Entity, row: dict, picker: Picker) -> dict:
    current = row.get(picker.key)
    embed = discord.Embed(title=f"⋆ {picker.label}", color=BLURPLE,
                          description=f"Current: {await picker_display(bot, picker, current)}")
    view = BaseView(bot, user_id)
    component, extract = None, None

    if picker.kind == "roles":
        defaults = [discord.Object(id=i, type=discord.Role) for i in jload(current)[:25]]
        component = discord.ui.RoleSelect(placeholder="Select roles…", min_values=0, max_values=25,
                                          default_values=defaults, row=0)
        extract = lambda: json.dumps([r.id for r in component.values])
    elif picker.kind == "role":
        defaults = [discord.Object(id=current, type=discord.Role)] if current else []
        component = discord.ui.RoleSelect(placeholder="Select a role…", min_values=1, max_values=1,
                                          default_values=defaults, row=0)
        extract = lambda: component.values[0].id
    elif picker.kind == "channel":
        component = discord.ui.ChannelSelect(placeholder="Select a channel…", min_values=1, max_values=1, row=0,
                                             channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        extract = lambda: component.values[0].id
    elif picker.kind == "category":
        component = discord.ui.ChannelSelect(placeholder="Select a channel category…", min_values=1, max_values=1,
                                             row=0, channel_types=[discord.ChannelType.category])
        extract = lambda: component.values[0].id
    else:
        ref = ENTITIES[picker.ref]
        rows = (await list_rows(bot, ref, guild_id))[:25]
        if not rows:
            embed.description += f"\n\nNo {ref.title.lower()} exist yet – create one first."
        else:
            chosen = set(jload(current)) if picker.kind == "refs" else {current}
            multi = picker.kind == "refs"
            component = discord.ui.Select(
                placeholder=f"Select {ref.singular}…", row=0, min_values=0 if multi else 1,
                max_values=min(len(rows), 10) if multi else 1,
                options=[discord.SelectOption(label=truncate(row_label(ref, r), 100), value=str(r["id"]),
                                              default=r["id"] in chosen) for r in rows])
            extract = (lambda: json.dumps([int(v) for v in component.values])) if multi else (lambda: int(component.values[0]))

    async def save(interaction: discord.Interaction, value):
        await bot.db.update(entity.table, row["id"], {picker.key: value})
        fresh = await get_row(bot, entity, row["id"], guild_id)
        warn = await after_change(bot, entity, fresh)
        await show(interaction, **await edit_screen(bot, user_id, guild_id, entity, row["id"],
                                                    notice=warn or "ʚ♡ɞ Updated."))

    if component is not None:
        async def picked(interaction: discord.Interaction):
            await save(interaction, extract())
        component.callback = picked
        view.add_item(component)

    async def clear(interaction: discord.Interaction):
        await save(interaction, "[]" if picker.kind in ("roles", "refs") else None)

    async def back(interaction: discord.Interaction):
        await show(interaction, **await edit_screen(bot, user_id, guild_id, entity, row["id"]))

    view.btn("Clear", clear, style=discord.ButtonStyle.danger, emoji="⋆", row=1)
    view.btn("Back", back, emoji="‹", row=1)
    return {"embeds": [embed], "view": view}
