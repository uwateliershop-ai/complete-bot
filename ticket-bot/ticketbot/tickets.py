"""Ticket creation and management (claim, transfer, users, rename, close, reopen, delete)."""
from __future__ import annotations

import asyncio
import json
import logging

import discord

from . import media, transcripts
from .ui import ConfirmView
from .util import (BLURPLE, GREEN, GREY, RED, channel_name, ensure_number, get_settings, is_admin,
                   is_staff, jload, render, truncate)

log = logging.getLogger(__name__)

ACTION_LABELS = {
    "ticket_created": "Ticket Created", "ticket_claimed": "Ticket Claimed", "ticket_unclaimed": "Ticket Unclaimed",
    "ticket_transferred": "Ticket Transferred", "user_added": "User Added", "user_removed": "User Removed",
    "ticket_renamed": "Ticket Renamed", "ticket_closed": "Ticket Closed", "ticket_reopened": "Ticket Reopened",
    "ticket_deleted": "Ticket Deleted",
    "order_lined_up": "Order Lined Up", "order_processing": "Order Processing", "order_done": "Order Done",
}


CLOSED_TITLE = "𐙚 Ticket closed"


def now_iso() -> str:
    return discord.utils.utcnow().isoformat()


def staff_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                       attach_files=True, embed_links=True, manage_messages=True)


def member_overwrite(can_send: bool = True) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(view_channel=True, send_messages=can_send, read_message_history=True,
                                       attach_files=can_send, embed_links=can_send)


async def log_event(bot, guild: discord.Guild, ticket: dict, action: str, actor_id: int | None,
                    details: str = "") -> None:
    """Persist an activity record and mirror it to the log channel (if configured)."""
    await bot.db.insert("ticket_logs", {
        "guild_id": guild.id, "ticket_id": ticket["id"], "ticket_number": ticket["number"],
        "action": action, "actor_id": actor_id, "details": details, "created_at": now_iso(),
    })
    settings = await get_settings(bot, guild.id)
    channel = guild.get_channel(settings["log_channel_id"] or 0)
    if isinstance(channel, discord.abc.Messageable):
        embed = discord.Embed(title=ACTION_LABELS.get(action, action), color=BLURPLE, timestamp=discord.utils.utcnow())
        embed.add_field(name="Ticket", value=f"#{ticket['number']:04d} (<#{ticket['channel_id']}>)")
        if actor_id:
            embed.add_field(name="By", value=f"<@{actor_id}>")
        if details:
            embed.add_field(name="Details", value=truncate(details, 1000), inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Entry point from panel buttons / dropdowns
# ---------------------------------------------------------------------------
async def check_limits(bot, user: discord.Member, category: dict, settings: dict) -> str | None:
    open_tickets = await bot.db.fetchall(
        "SELECT * FROM tickets WHERE guild_id = ? AND owner_id = ? AND status = 'open'",
        (user.guild.id, user.id),
    )
    if settings["prevent_duplicates"]:
        for t in open_tickets:
            if t["category_id"] == category["id"] and user.guild.get_channel(t["channel_id"]):
                return f"⋆｡°✩ You already have an open ticket in this category: <#{t['channel_id']}>"
    limit = settings["max_open_tickets"]
    if limit and len(open_tickets) >= limit:
        return f"⋆｡°✩ You've reached the maximum of **{limit}** open tickets. Please close one first."
    return None


async def handle_entry(interaction: discord.Interaction, bot, *, category_id, action: str, form_id, panel_id) -> None:
    db = bot.db
    panel = await db.get("panels", panel_id) if panel_id else None
    if panel is not None and not panel["enabled"]:
        return await interaction.response.send_message("⋆｡°✩ This ticket panel is currently disabled.", ephemeral=True)
    category = await db.get("categories", category_id) if category_id else None
    if not category or not category["enabled"]:
        return await interaction.response.send_message("⋆｡°✩ This ticket option is currently unavailable.", ephemeral=True)
    settings = await get_settings(bot, interaction.guild_id)
    error = await check_limits(bot, interaction.user, category, settings)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)

    # create  -> the category's own form (if any)   form -> the button's form   category -> straight in, no form
    chosen_form = None
    if action == "form":
        chosen_form = form_id or category["form_id"]
    elif action == "create":
        chosen_form = category["form_id"]
    questions, form_name = [], "Ticket form"
    if chosen_form:
        questions = await db.where("form_questions", order="position, id", form_id=chosen_form)
        form = await db.get("forms", chosen_form)
        form_name = form["name"] if form else form_name
    if questions:
        return await interaction.response.send_modal(TicketFormModal(bot, category, panel_id, questions, form_name))
    await interaction.response.defer(ephemeral=True, thinking=True)
    await create_ticket(interaction, bot, category, panel_id, [])


class TicketFormModal(discord.ui.Modal):
    def __init__(self, bot, category: dict, panel_id, questions: list[dict], form_name: str):
        super().__init__(title=truncate(form_name, 45))
        self.bot, self.category, self.panel_id = bot, category, panel_id
        self.inputs: list[tuple[dict, discord.ui.TextInput]] = []
        for q in questions[:5]:
            paragraph = q["style"] == "paragraph"
            ti = discord.ui.TextInput(
                label=truncate(q["label"], 45),
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                placeholder=q["placeholder"] or None,
                required=bool(q["required"]),
                max_length=1000 if paragraph else 300,
            )
            self.add_item(ti)
            self.inputs.append((q, ti))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = [{"q": q["label"], "a": ti.value or "—"} for q, ti in self.inputs]
        await create_ticket(interaction, self.bot, self.category, self.panel_id, answers)

    async def on_error(self, interaction, error) -> None:
        log.exception("Ticket form error", exc_info=error)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
async def create_ticket(interaction: discord.Interaction, bot, category: dict, panel_id, answers: list[dict]) -> None:
    guild, user, db = interaction.guild, interaction.user, bot.db
    lock = bot.user_locks.setdefault((guild.id, user.id), asyncio.Lock())
    async with lock:                                   # stops double-click duplicates
        settings = await get_settings(bot, guild.id)
        error = await check_limits(bot, user, category, settings)
        if error:
            return await interaction.followup.send(error, ephemeral=True)

        number = await db.next_ticket_number(guild.id)
        staff_ids = set(jload(settings["staff_role_ids"])) | set(jload(category["staff_role_ids"]))
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                                  manage_messages=True, read_message_history=True,
                                                  embed_links=True, attach_files=True),
            user: member_overwrite(),
        }
        staff_roles = []
        for rid in staff_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = staff_overwrite()
                staff_roles.append(role)

        parent = guild.get_channel(category["discord_category_id"] or settings["ticket_category_id"] or 0)
        if not isinstance(parent, discord.CategoryChannel):
            parent = None
        fmt = category["naming_format"] or settings["naming_format"]
        name = ensure_number(channel_name(fmt, number=number, username=user.name, category=category["name"]), number)
        try:
            channel = await guild.create_text_channel(
                name, category=parent, overwrites=overwrites,
                topic=f"Ticket #{number:04d} | Owner: {user} ({user.id}) | {category['name']}",
                reason=f"Ticket #{number:04d} opened by {user}",
            )
        except discord.HTTPException as exc:
            log.warning("Ticket channel creation failed: %s", exc)
            return await interaction.followup.send(
                "⋆｡°✩ I couldn't create the ticket channel. Please tell an administrator "
                "(I need **Manage Channels** and **Manage Roles**, and the category may be full).",
                ephemeral=True)

        ticket_id = await db.insert("tickets", {
            "guild_id": guild.id, "number": number, "channel_id": channel.id, "owner_id": user.id,
            "category_id": category["id"], "panel_id": panel_id, "status": "open",
            "form_answers": json.dumps(answers), "created_at": now_iso(),
        })
    ticket = await db.get("tickets", ticket_id)

    welcome = render(category["welcome_message"] or settings["welcome_message"],
                     user=user.mention, user_name=user.display_name, category=category["name"],
                     number=f"{number:04d}", guild=guild.name)
    rich = media.rich_text(guild, welcome)             # :server_emoji: and {sticker:Name}
    welcome = rich.text
    embed = discord.Embed(title=f"୨୧ Ticket #{number:04d} – {category['name']}", description=welcome[:4000],
                          color=BLURPLE, timestamp=discord.utils.utcnow())
    embed.set_footer(text=f"Opened by {user.display_name}", icon_url=user.display_avatar.url)
    for ans in answers:
        embed.add_field(name=truncate(ans["q"], 256), value=truncate(ans["a"], 1024), inline=False)
    mentions = user.mention
    if category["mention_staff"] and staff_roles:
        mentions += " " + " ".join(r.mention for r in staff_roles)
    try:
        await channel.send(content=mentions, embed=embed, view=TicketControlView(bot),
                           **({"stickers": rich.stickers} if rich.stickers else {}))
    except discord.HTTPException:
        if not rich.stickers:
            raise
        await channel.send(content=mentions, embed=embed, view=TicketControlView(bot))   # without the stickers
    await log_event(bot, guild, ticket, "ticket_created", user.id,
                    f"Category: {category['name']} | Channel: #{channel.name}")
    try:                                               # shop order flow (pledge -> form -> payment -> queue)
        from . import orders
        await orders.on_ticket_created(bot, channel, ticket, category, user, answers)
    except Exception:
        log.exception("Order flow failed to start")
    await interaction.followup.send(f"ʚ♡ɞ Your ticket has been created: {channel.mention}", ephemeral=True)


# ---------------------------------------------------------------------------
# Context helpers for the in-ticket buttons
# ---------------------------------------------------------------------------
async def load_ctx(interaction: discord.Interaction, bot, *, need_open: bool = True):
    ticket = await bot.db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
    if not ticket:
        await interaction.response.send_message("⋆｡°✩ This channel isn't a tracked ticket.", ephemeral=True)
        return None
    if need_open and ticket["status"] != "open":
        await interaction.response.send_message("⋆｡°✩ This ticket is closed.", ephemeral=True)
        return None
    category = await bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
    settings = await get_settings(bot, interaction.guild_id)
    return ticket, category, settings


async def staff_ctx(interaction, bot, *, need_open: bool = True):
    ctx = await load_ctx(interaction, bot, need_open=need_open)
    if ctx is None:
        return None
    if not is_staff(interaction.user, ctx[2], ctx[1]):
        await interaction.response.send_message("⋆｡°✩ Only ticket staff can do that.", ephemeral=True)
        return None
    return ctx


# ---------------------------------------------------------------------------
# Persistent in-ticket views
# ---------------------------------------------------------------------------
class TicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="♡ Claim", style=discord.ButtonStyle.success, custom_id="tb:t:claim", row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await staff_ctx(interaction, self.bot)
        if not ctx:
            return
        ticket = ctx[0]
        if ticket["claimed_by"]:
            who = "you" if ticket["claimed_by"] == interaction.user.id else f"<@{ticket['claimed_by']}>"
            return await interaction.response.send_message(f"⋆｡°✩ This ticket is already claimed by {who}.", ephemeral=True)
        await self.bot.db.update("tickets", ticket["id"], {"claimed_by": interaction.user.id})
        await log_event(self.bot, interaction.guild, ticket, "ticket_claimed", interaction.user.id)
        await interaction.response.send_message(embed=discord.Embed(
            description=f"♡ {interaction.user.mention} has claimed this ticket.", color=GREEN))

    @discord.ui.button(label="⟲ Unclaim", style=discord.ButtonStyle.secondary, custom_id="tb:t:unclaim", row=0)
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await staff_ctx(interaction, self.bot)
        if not ctx:
            return
        ticket, _, settings = ctx
        if not ticket["claimed_by"]:
            return await interaction.response.send_message("⋆｡°✩ This ticket isn't claimed.", ephemeral=True)
        if ticket["claimed_by"] != interaction.user.id and not is_admin(interaction.user, settings):
            return await interaction.response.send_message("⋆｡°✩ Only the claimer or an admin can unclaim.", ephemeral=True)
        await self.bot.db.update("tickets", ticket["id"], {"claimed_by": None})
        await log_event(self.bot, interaction.guild, ticket, "ticket_unclaimed", interaction.user.id)
        await interaction.response.send_message(embed=discord.Embed(
            description=f"⟲ {interaction.user.mention} released this ticket.", color=GREY))

    @discord.ui.button(label="⇄ Transfer", style=discord.ButtonStyle.primary, custom_id="tb:t:transfer", row=0)
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await staff_ctx(interaction, self.bot)
        if not ctx:
            return
        cats = await self.bot.db.where("categories", guild_id=interaction.guild_id, enabled=1)
        await interaction.response.send_message(
            "Choose a new assignee and/or a new support team (category), then confirm.",
            view=TransferView(self.bot, ctx[0]["id"], cats[:25], interaction.user.id), ephemeral=True)

    @discord.ui.button(label="𐙚 Close", style=discord.ButtonStyle.danger, custom_id="tb:t:close", row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await load_ctx(interaction, self.bot)
        if not ctx:
            return
        ticket, category, settings = ctx
        owner_ok = interaction.user.id == ticket["owner_id"] and (category is None or category["owner_can_close"])
        if not (owner_ok or is_staff(interaction.user, settings, category)):
            return await interaction.response.send_message("⋆｡°✩ You can't close this ticket.", ephemeral=True)
        if settings["close_confirmation"]:
            async def yes(i: discord.Interaction):
                await i.response.edit_message(content="𐙚 Closing ticket…", view=None)
                await close_ticket(self.bot, i.channel, ticket["id"], i.user)
            return await interaction.response.send_message(
                "Are you sure you want to close this ticket?", ephemeral=True,
                view=ConfirmView(interaction.user.id, yes, yes_label="Close ticket"))
        await interaction.response.defer(ephemeral=True)
        await close_ticket(self.bot, interaction.channel, ticket["id"], interaction.user)
        await interaction.followup.send("𐙚 Ticket closed.", ephemeral=True)

    @discord.ui.button(label="୨୧ Transcript", style=discord.ButtonStyle.secondary, custom_id="tb:t:transcript", row=0)
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_manual_transcript(interaction, self.bot)

    @discord.ui.button(label="+ Add user", style=discord.ButtonStyle.secondary, custom_id="tb:t:add", row=1)
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await staff_ctx(interaction, self.bot):
            return
        await interaction.response.send_message("Select the members to add:", ephemeral=True,
                                                view=UserPickView(self.bot, "add", interaction.user.id))

    @discord.ui.button(label="− Remove user", style=discord.ButtonStyle.secondary, custom_id="tb:t:remove", row=1)
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await staff_ctx(interaction, self.bot):
            return
        await interaction.response.send_message("Select the members to remove:", ephemeral=True,
                                                view=UserPickView(self.bot, "remove", interaction.user.id))

    @discord.ui.button(label="✎ Rename", style=discord.ButtonStyle.secondary, custom_id="tb:t:rename", row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await staff_ctx(interaction, self.bot):
            return
        await interaction.response.send_modal(RenameModal(self.bot, interaction.channel.name))


class ClosedView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="𐙚 Reopen", style=discord.ButtonStyle.success, custom_id="tb:t:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await staff_ctx(interaction, self.bot, need_open=False)
        if not ctx:
            return
        ticket, category, _ = ctx
        if ticket["status"] != "closed":
            return await interaction.response.send_message("⋆｡°✩ This ticket isn't closed.", ephemeral=True)
        await interaction.response.edit_message(view=None)
        await reopen_ticket(self.bot, interaction.channel, ticket, category, interaction.user)

    @discord.ui.button(label="୨୧ Transcript", style=discord.ButtonStyle.secondary, custom_id="tb:t:transcript2")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_manual_transcript(interaction, self.bot)

    @discord.ui.button(label="⋆ Delete", style=discord.ButtonStyle.danger, custom_id="tb:t:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await staff_ctx(interaction, self.bot, need_open=False)
        if not ctx:
            return
        ticket = ctx[0]

        async def yes(i: discord.Interaction):
            await i.response.edit_message(content="⋆ Saving transcript and deleting channel…", view=None)
            await delete_ticket(self.bot, i.channel, ticket["id"], i.user, i)

        await interaction.response.send_message(
            "⋆｡°✩ Delete this ticket channel permanently? A transcript is saved first.", ephemeral=True,
            view=ConfirmView(interaction.user.id, yes, yes_label="Delete ticket"))


# ---------------------------------------------------------------------------
# Sub-dialogs
# ---------------------------------------------------------------------------
class TransferView(discord.ui.View):
    def __init__(self, bot, ticket_id: int, categories: list[dict], actor_id: int):
        super().__init__(timeout=300)
        self.bot, self.ticket_id, self.actor_id = bot, ticket_id, actor_id
        self.member = None
        self.category_id = None
        users = discord.ui.UserSelect(placeholder="New assignee (staff member)", min_values=0, max_values=1, row=0)
        users.callback = self.pick_user
        self.users = users
        self.add_item(users)
        if categories:
            cats = discord.ui.Select(
                placeholder="New support team (category)", min_values=0, max_values=1, row=1,
                options=[discord.SelectOption(label=truncate(c["name"], 100), value=str(c["id"])) for c in categories])
            cats.callback = self.pick_category
            self.cats = cats
            self.add_item(cats)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.actor_id

    async def pick_user(self, interaction: discord.Interaction):
        self.member = self.users.values[0] if self.users.values else None
        await interaction.response.defer()

    async def pick_category(self, interaction: discord.Interaction):
        self.category_id = int(self.cats.values[0]) if self.cats.values else None
        await interaction.response.defer()

    @discord.ui.button(label="ʚ♡ɞ Confirm transfer", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.member and not self.category_id:
            return await interaction.response.send_message("Choose an assignee or a category first.", ephemeral=True)
        bot, db, guild, channel = self.bot, self.bot.db, interaction.guild, interaction.channel
        ticket = await db.get("tickets", self.ticket_id)
        settings = await get_settings(bot, guild.id)
        old_cat = await db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        new_cat = await db.get("categories", self.category_id) if self.category_id else old_cat
        notes: list[str] = []

        member = guild.get_member(self.member.id) if self.member else None
        if self.member and not is_staff(member, settings, new_cat):
            return await interaction.response.send_message(f"⋆｡°✩ {self.member.mention} isn't ticket staff.", ephemeral=True)

        await interaction.response.edit_message(content="⇄ Transferring…", view=None)
        if self.category_id and self.category_id != ticket["category_id"]:
            keep = set(jload(settings["staff_role_ids"])) | set(jload(new_cat["staff_role_ids"]))
            for rid in set(jload(old_cat["staff_role_ids"] if old_cat else "[]")) - keep:
                role = guild.get_role(rid)
                if role:
                    await channel.set_permissions(role, overwrite=None)
            for rid in jload(new_cat["staff_role_ids"]):
                role = guild.get_role(rid)
                if role:
                    await channel.set_permissions(role, overwrite=staff_overwrite())
            parent = guild.get_channel(new_cat["discord_category_id"] or 0)
            if isinstance(parent, discord.CategoryChannel):
                await channel.edit(category=parent)
            await db.update("tickets", ticket["id"], {"category_id": new_cat["id"]})
            notes.append(f"team: {old_cat['name'] if old_cat else '—'} → {new_cat['name']}")
        if member:
            await channel.set_permissions(member, overwrite=staff_overwrite())
            prev = f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "nobody"
            await db.update("tickets", ticket["id"], {"claimed_by": member.id})
            notes.append(f"assignee: {prev} → {member.mention}")

        ticket = await db.get("tickets", ticket["id"])
        await log_event(bot, guild, ticket, "ticket_transferred", interaction.user.id, "; ".join(notes))
        await channel.send(embed=discord.Embed(
            description=f"⇄ Ticket transferred by {interaction.user.mention}\n" + "\n".join(f"• {n}" for n in notes),
            color=BLURPLE))
        await interaction.edit_original_response(content="ʚ♡ɞ Ticket transferred.", view=None)


class UserPickView(discord.ui.View):
    def __init__(self, bot, mode: str, actor_id: int):
        super().__init__(timeout=120)
        self.bot, self.mode, self.actor_id = bot, mode, actor_id
        select = discord.ui.UserSelect(placeholder="Pick members…", min_values=1, max_values=10)
        select.callback = self.picked
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.actor_id

    async def picked(self, interaction: discord.Interaction):
        bot, db, guild, channel = self.bot, self.bot.db, interaction.guild, interaction.channel
        ticket = await db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel.id,))
        if not ticket or ticket["status"] != "open":
            return await interaction.response.edit_message(content="⋆｡°✩ This ticket is no longer open.", view=None)
        settings = await get_settings(bot, guild.id)
        done, skipped = [], []
        for user in self.select.values:
            member = guild.get_member(user.id)
            if member is None or member.bot:
                skipped.append(f"{user.mention} (not a server member / bot)")
                continue
            if self.mode == "add":
                await channel.set_permissions(member, overwrite=member_overwrite())
                await db.execute("INSERT OR IGNORE INTO ticket_participants (ticket_id, user_id, added_by, added_at) "
                                 "VALUES (?, ?, ?, ?)", (ticket["id"], member.id, interaction.user.id, now_iso()))
                await log_event(bot, guild, ticket, "user_added", interaction.user.id, f"Added {member} ({member.id})")
                await channel.send(f"+ {member.mention} was added to this ticket by {interaction.user.mention}.")
            else:
                if member.id == ticket["owner_id"] or is_staff(member, settings):
                    skipped.append(f"{member.mention} (owner/staff can't be removed)")
                    continue
                await channel.set_permissions(member, overwrite=None)
                await db.execute("DELETE FROM ticket_participants WHERE ticket_id = ? AND user_id = ?",
                                 (ticket["id"], member.id))
                await log_event(bot, guild, ticket, "user_removed", interaction.user.id, f"Removed {member} ({member.id})")
                await channel.send(f"− {member.mention} was removed from this ticket by {interaction.user.mention}.")
            done.append(member.mention)
        text = (f"ʚ♡ɞ {'Added' if self.mode == 'add' else 'Removed'}: {', '.join(done)}" if done else "Nothing changed.")
        if skipped:
            text += "\n⋆｡°✩ Skipped: " + ", ".join(skipped)
        await interaction.response.edit_message(content=text, view=None)


class RenameModal(discord.ui.Modal, title="Rename ticket"):
    def __init__(self, bot, current: str):
        super().__init__()
        self.bot = bot
        self.name = discord.ui.TextInput(label="New channel name", default=current, max_length=90)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ticket = await self.bot.db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
        if not ticket:
            return await interaction.response.send_message("⋆｡°✩ Not a ticket channel.", ephemeral=True)
        new = ensure_number(channel_name(self.name.value, number=ticket["number"], username="user", category="ticket"),
                            ticket["number"])
        old = interaction.channel.name
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.wait_for(interaction.channel.edit(name=new, reason=f"Renamed by {interaction.user}"), 10)
        except (asyncio.TimeoutError, discord.HTTPException):
            return await interaction.followup.send(
                "⋆｡°✩ Discord rate-limits channel renames (2 per 10 minutes). Please try again later.", ephemeral=True)
        await log_event(self.bot, interaction.guild, ticket, "ticket_renamed", interaction.user.id, f"#{old} → #{new}")
        await interaction.followup.send(f"ʚ♡ɞ Renamed to **#{new}** (ticket number preserved).", ephemeral=True)


# ---------------------------------------------------------------------------
# Close / reopen / delete
# ---------------------------------------------------------------------------
async def _restrict_owner(guild, channel, ticket_id: int, owner_id: int, bot, *, can_send: bool) -> None:
    ids = {owner_id} | {p["user_id"] for p in await bot.db.where("ticket_participants", ticket_id=ticket_id)}
    for uid in ids:
        member = guild.get_member(uid)
        if member:
            await channel.set_permissions(member, overwrite=member_overwrite(can_send))


async def close_ticket(bot, channel: discord.TextChannel, ticket_id: int, closer: discord.Member) -> None:
    db, guild = bot.db, channel.guild
    ticket = await db.get("tickets", ticket_id)
    if not ticket or ticket["status"] != "open":
        return
    await db.update("tickets", ticket_id, {"status": "closed", "closed_at": now_iso(), "closed_by": closer.id})
    ticket = await db.get("tickets", ticket_id)
    await _restrict_owner(guild, channel, ticket_id, ticket["owner_id"], bot, can_send=False)
    await log_event(bot, guild, ticket, "ticket_closed", closer.id)

    settings = await get_settings(bot, guild.id)
    note = ""
    if settings["auto_transcript"]:
        try:
            await transcripts.create_transcript(bot, channel, ticket, archive=True)
            note = "\n୨୧ A transcript has been saved."
        except Exception:                                   # never block closing on a transcript failure
            log.exception("Auto transcript failed")
            note = "\n⋆｡°✩ Transcript generation failed – staff can retry with the Transcript button."
    embed = discord.Embed(title=CLOSED_TITLE, color=RED, timestamp=discord.utils.utcnow(),
                          description=f"Closed by {closer.mention}.{note}\nStaff can reopen or delete this ticket below.")
    await channel.send(embed=embed, view=ClosedView(bot))


async def reopen_ticket(bot, channel: discord.TextChannel, ticket: dict, category, user: discord.Member) -> None:
    db, guild = bot.db, channel.guild
    await db.update("tickets", ticket["id"], {"status": "open", "closed_at": None, "closed_by": None})
    ticket = await db.get("tickets", ticket["id"])
    await _restrict_owner(guild, channel, ticket["id"], ticket["owner_id"], bot, can_send=True)
    await log_event(bot, guild, ticket, "ticket_reopened", user.id)
    await channel.send(content=f"<@{ticket['owner_id']}>", embed=discord.Embed(
        title="𐙚 Ticket reopened", description=f"Reopened by {user.mention}.", color=GREEN,
        timestamp=discord.utils.utcnow()), view=TicketControlView(bot))


async def delete_ticket(bot, channel: discord.TextChannel, ticket_id: int, user: discord.Member,
                        interaction: discord.Interaction) -> None:
    db, guild = bot.db, channel.guild
    ticket = await db.get("tickets", ticket_id)
    if not await db.fetchone("SELECT id FROM transcripts WHERE ticket_id = ? LIMIT 1", (ticket_id,)):
        try:
            await transcripts.create_transcript(bot, channel, ticket, archive=True)
        except Exception:
            log.exception("Pre-delete transcript failed")
            return await interaction.edit_original_response(
                content="⋆｡°✩ Transcript generation failed, so the ticket was **not** deleted. Try again.")
    await db.update("tickets", ticket_id, {"status": "deleted", "closed_at": ticket["closed_at"] or now_iso(),
                                           "closed_by": ticket["closed_by"] or user.id})
    await log_event(bot, guild, ticket, "ticket_deleted", user.id)
    await channel.send("⋆ This ticket will be deleted in 5 seconds…")
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket #{ticket['number']:04d} deleted by {user}")
    except discord.HTTPException:
        log.warning("Could not delete ticket channel %s", channel.id)


async def send_manual_transcript(interaction: discord.Interaction, bot) -> None:
    ctx = await staff_ctx(interaction, bot, need_open=False)
    if not ctx:
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        path = await transcripts.create_transcript(bot, interaction.channel, ctx[0], archive=True)
    except Exception:
        log.exception("Manual transcript failed")
        return await interaction.followup.send("⋆｡°✩ Could not generate the transcript.", ephemeral=True)
    await interaction.followup.send("୨୧ Transcript generated.", file=discord.File(path), ephemeral=True)
