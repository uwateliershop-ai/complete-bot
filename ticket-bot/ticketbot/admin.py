"""Admin & staff slash commands that complement /ticket quickpanel and /ticket dashboard.

Panels   (admin)  : /ticket panels · publish · unpublish
Settings (admin)  : /ticket settings
Tickets  (admin)  : /ticket list
Tickets  (staff)  : /ticket info · close · reopen · delete     (number optional inside a ticket channel)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

import discord
from discord import app_commands

from . import panels, tickets
from .ui import ConfirmView
from .util import BLURPLE, ERR, OK, QUICK_PREFIX, get_settings, is_admin, is_quick, is_staff, jload, truncate

LIST_LIMIT = 25
EMBED_BUDGET = 3800            # characters of an embed description we are willing to fill


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def panel_label(guild: discord.Guild, panel: dict) -> str:
    """Human friendly panel name (quick panels are stored as 'quick-<channel id>')."""
    if not is_quick(panel):
        return panel["name"]
    raw = panel["name"][len(QUICK_PREFIX):]
    channel = guild.get_channel(int(raw)) if raw.isdigit() else None
    return f"Quick panel · #{channel.name}" if channel else f"Quick panel · {raw}"


async def resolve_panel(bot, guild: discord.Guild, text: str) -> dict | None:
    """Find a panel from an autocomplete value (its id) or from a typed name."""
    text = (text or "").strip()
    rows = await bot.db.where("panels", order="id", guild_id=guild.id)
    if text.isdigit():
        for row in rows:
            if row["id"] == int(text):
                return row
    low = text.lower()
    for row in rows:
        if low in (row["name"].lower(), panel_label(guild, row).lower()):
            return row
    return None


async def _count(db, sql: str, params: tuple) -> int:
    row = await db.fetchone(sql, params)
    return row["n"] if row else 0


def _stamp(iso: str | None) -> str:
    """ISO timestamp -> Discord timestamp markup (shows in each viewer's own time zone)."""
    if not iso:
        return "—"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    ts = int(moment.timestamp())
    return f"<t:{ts}:f> (<t:{ts}:R>)"


def _ticket_channel(guild: discord.Guild, ticket: dict):
    channel = guild.get_channel(ticket["channel_id"])
    return channel if isinstance(channel, discord.TextChannel) else None


async def strip_closed_buttons(bot, channel: discord.TextChannel) -> None:
    """Remove the buttons of the latest 'Ticket closed' message (the Reopen button does the same)."""
    me = getattr(bot, "user", None)
    try:
        async for message in channel.history(limit=50):
            if (me is not None and message.author.id == me.id and message.embeds
                    and message.embeds[0].title == tickets.CLOSED_TITLE):
                await message.edit(view=None)
                return
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Publishing (also used by the tests)
# ---------------------------------------------------------------------------
async def publish_panel(bot, guild: discord.Guild, panel: dict, channel=None) -> str:
    """(Re)post a saved panel: its pictures first, then the panel itself. The previous copy is only removed once
    the new one is up."""
    if is_quick(panel) and channel is not None and channel.id != (panel["channel_id"] or 0):
        return (f"{ERR} Quick panels stay in their own channel. To move it, run `/ticket quickpanel` "
                f"in the new channel.")
    target = channel or guild.get_channel(panel["channel_id"] or 0)
    if not isinstance(target, discord.abc.Messageable):
        return f"{ERR} This panel has no destination channel. Pass `channel`, or set one in `/ticket dashboard`."
    return await panels.publish(bot, guild, panel["id"], channel)


async def unpublish_panel(bot, panel: dict, label: str) -> str:
    """Delete a panel's messages but keep all of its settings."""
    if not panel["message_id"]:
        return f"{ERR} **{label}** isn't published."
    reachable = bot.get_channel(panel["message_channel_id"] or 0) is not None
    await panels.delete_published(bot, panel)
    await bot.db.update("panels", panel["id"], {"message_id": None, "message_channel_id": None,
                                                "decor_message_id": None, "image_message_ids": "[]"})
    note = "" if reachable else (f"\n{ERR} I couldn't reach the old message's channel. "
                                 "Delete the message by hand if it is still there.")
    return f"{OK} **{label}** unpublished. Settings kept. Use `/ticket publish` to post it again.{note}"


def make_panel_autocomplete(bot):
    """Autocomplete for every `panel` option (admins only)."""
    async def panel_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        settings = await get_settings(bot, interaction.guild_id)
        if not is_admin(interaction.user, settings):
            return []
        needle = (current or "").lower()
        out = []
        for row in await bot.db.where("panels", order="id", guild_id=interaction.guild_id):
            label = panel_label(interaction.guild, row)
            if needle in label.lower():
                out.append(app_commands.Choice(name=truncate(label, 100), value=str(row["id"])))
        return out[:25]
    return panel_autocomplete


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------
def add_admin_commands(bot, group: app_commands.Group, admin_gate) -> None:
    db = bot.db
    panel_autocomplete = make_panel_autocomplete(bot)

    async def find_ticket(interaction: discord.Interaction, number: Optional[int]):
        """The ticket a command is about: by number, or the ticket channel it is run in.
        Sends the error reply itself and returns None when there is nothing usable."""
        if number is None:
            ticket = await db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
            if not ticket:
                await interaction.response.send_message(
                    f"{ERR} Run this inside a ticket channel, or pass the ticket `number`.", ephemeral=True)
                return None
        else:
            ticket = await db.fetchone("SELECT * FROM tickets WHERE guild_id = ? AND number = ?",
                                       (interaction.guild_id, number))
            if not ticket:
                await interaction.response.send_message(f"{ERR} There is no ticket #{number:04d}.", ephemeral=True)
                return None
        category = await db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        settings = await get_settings(bot, interaction.guild_id)
        return ticket, category, settings

    # ---- panels ----------------------------------------------------------
    @group.command(name="panels", description="List every ticket panel and where it is posted.")
    async def list_panels(interaction: discord.Interaction):
        if not await admin_gate(interaction, bot):
            return
        guild = interaction.guild
        rows = await db.where("panels", order="id", guild_id=guild.id)
        embed = discord.Embed(title="୨୧ Ticket panels", color=BLURPLE)
        if not rows:
            embed.description = "No panels yet. Run `/ticket quickpanel` to create one."
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        lines = []
        for index, row in enumerate(rows, start=1):
            options = await _count(
                db, "SELECT COUNT(*) AS n FROM dropdown_options o JOIN panel_dropdowns d ON d.id = o.dropdown_id "
                    "WHERE d.panel_id = ?", (row["id"],))
            buttons = await _count(db, "SELECT COUNT(*) AS n FROM panel_buttons WHERE panel_id = ?", (row["id"],))
            if row["message_id"] and row["message_channel_id"]:
                link = f"https://discord.com/channels/{guild.id}/{row['message_channel_id']}/{row['message_id']}"
                where = f"[posted]({link}) in <#{row['message_channel_id']}>"
                mark = OK
            else:
                target = f"<#{row['channel_id']}>" if row["channel_id"] else "no channel set"
                where = f"not posted · target {target}"
                mark = ERR
            extras = [f"{options + buttons} option{'s' if options + buttons != 1 else ''}"]
            if not row["enabled"]:
                extras.append("disabled")
            line = truncate(f"`{index}.` {mark} **{truncate(panel_label(guild, row), 60)}** · {where} · "
                            f"{' · '.join(extras)}", 400)
            if sum(len(x) + 1 for x in lines) + len(line) > EMBED_BUDGET:
                break
            lines.append(line)
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Showing {len(lines)} of {len(rows)} • /ticket publish posts or re-posts a panel")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="publish", description="Post (or re-post) a saved panel. The old message is replaced.")
    @app_commands.describe(panel="Which panel to post",
                           channel="Where to post it (default: the panel's own channel)")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def publish(interaction: discord.Interaction, panel: str, channel: Optional[discord.TextChannel] = None):
        if not await admin_gate(interaction, bot):
            return
        row = await resolve_panel(bot, interaction.guild, panel)
        if row is None:
            return await interaction.response.send_message(
                f"{ERR} I couldn't find that panel. Pick one from the list, or see `/ticket panels`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        status = await publish_panel(bot, interaction.guild, row, channel)
        if status.startswith(OK) and not row["enabled"]:
            status += (f"\n{ERR} This panel is **disabled**, so its menu is greyed out. "
                       "Enable it in `/ticket dashboard` → Panel Management.")
        await interaction.followup.send(status, ephemeral=True)

    @group.command(name="unpublish", description="Take a panel down but keep all of its settings.")
    @app_commands.describe(panel="Which panel to take down")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def unpublish(interaction: discord.Interaction, panel: str):
        if not await admin_gate(interaction, bot):
            return
        row = await resolve_panel(bot, interaction.guild, panel)
        if row is None:
            return await interaction.response.send_message(
                f"{ERR} I couldn't find that panel. Pick one from the list, or see `/ticket panels`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            await unpublish_panel(bot, row, panel_label(interaction.guild, row)), ephemeral=True)

    # ---- settings overview -------------------------------------------------
    @group.command(name="settings", description="Show the current ticket settings at a glance.")
    async def settings_command(interaction: discord.Interaction):
        if not await admin_gate(interaction, bot):
            return
        gid = interaction.guild_id
        s = await get_settings(bot, gid)

        def roles(value) -> str:
            return truncate(" ".join(f"<@&{i}>" for i in jload(value)) or "—", 1000)

        def chan(value) -> str:
            return f"<#{value}>" if value else "—"

        def flag(value) -> str:
            return "on" if value else "off"

        embed = discord.Embed(title="୨୧ Ticket settings", color=BLURPLE,
                              description="Administrators and the server owner always have full access.")
        embed.add_field(name="Staff roles", value=roles(s["staff_role_ids"]))
        embed.add_field(name="Admin roles", value=roles(s["admin_role_ids"]))
        embed.add_field(name="Ticket folder", value=chan(s["ticket_category_id"]))
        embed.add_field(name="Archive channel", value=chan(s["archive_channel_id"]))
        embed.add_field(name="Log channel", value=chan(s["log_channel_id"]))
        embed.add_field(name="Max open per member",
                        value=str(s["max_open_tickets"]) if s["max_open_tickets"] else "unlimited")
        embed.add_field(name="Duplicate prevention", value=flag(s["prevent_duplicates"]))
        embed.add_field(name="Close confirmation", value=flag(s["close_confirmation"]))
        embed.add_field(name="Auto transcripts", value=flag(s["auto_transcript"]))
        embed.add_field(name="Name format", value=truncate(f"`{s['naming_format']}`", 200))
        embed.add_field(name="Welcome message", value=truncate(s["welcome_message"] or "—", 300), inline=False)
        panels_n = await _count(db, "SELECT COUNT(*) AS n FROM panels WHERE guild_id = ?", (gid,))
        cats_n = await _count(db, "SELECT COUNT(*) AS n FROM categories WHERE guild_id = ?", (gid,))
        forms_n = await _count(db, "SELECT COUNT(*) AS n FROM forms WHERE guild_id = ?", (gid,))
        open_n = await _count(db, "SELECT COUNT(*) AS n FROM tickets WHERE guild_id = ? AND status = 'open'", (gid,))
        embed.add_field(name="Totals", inline=False,
                        value=f"{panels_n} panels · {cats_n} categories · {forms_n} forms · {open_n} open tickets")
        embed.set_footer(text="Change these in /ticket dashboard")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- ticket overview ---------------------------------------------------
    @group.command(name="list", description="List tickets, newest first.")
    @app_commands.describe(status="Which tickets to show (default: open)",
                           user="Only tickets opened by this member")
    async def list_tickets(interaction: discord.Interaction,
                           status: Literal["open", "closed", "deleted", "all"] = "open",
                           user: Optional[discord.User] = None):
        if not await admin_gate(interaction, bot):
            return
        where, params = ["guild_id = ?"], [interaction.guild_id]
        if status != "all":
            where.append("status = ?")
            params.append(status)
        if user is not None:
            where.append("owner_id = ?")
            params.append(user.id)
        clause = " AND ".join(where)
        total = await _count(db, f"SELECT COUNT(*) AS n FROM tickets WHERE {clause}", tuple(params))
        rows = await db.fetchall(
            f"SELECT * FROM tickets WHERE {clause} ORDER BY number DESC LIMIT {LIST_LIMIT}", tuple(params))
        names = {c["id"]: c["name"] for c in await db.where("categories", guild_id=interaction.guild_id)}

        title = "୨୧ Tickets" if status == "all" else f"୨୧ {status.capitalize()} tickets"
        header = f"**{total}** ticket{'s' if total != 1 else ''}" + (f" opened by <@{user.id}>" if user else "")
        lines = []
        for t in rows:
            place = "deleted" if t["status"] == "deleted" else f"<#{t['channel_id']}>"
            line = (f"`#{t['number']:04d}` {t['status']} · {place} · "
                    f"{truncate(names.get(t['category_id'], '—'), 30)} · <@{t['owner_id']}>")
            if t["claimed_by"]:
                line += f" · claimed by <@{t['claimed_by']}>"
            if len(header) + sum(len(x) + 1 for x in lines) + len(line) > EMBED_BUDGET:
                break
            lines.append(line)
        body = "\n".join(lines) if lines else "No tickets match."
        embed = discord.Embed(title=title, description=f"{header}\n\n{body}", color=BLURPLE)
        if lines:
            embed.set_footer(text=f"Showing {len(lines)} of {total} • /ticket info <number> for details")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="info", description="Show details about a ticket (this channel's ticket by default).")
    @app_commands.describe(number="Ticket number (leave empty inside a ticket channel)")
    async def info(interaction: discord.Interaction, number: Optional[int] = None):
        ctx = await find_ticket(interaction, number)
        if not ctx:
            return
        ticket, category, settings = ctx
        if not is_staff(interaction.user, settings, category):
            return await interaction.response.send_message(f"{ERR} Staff only.", ephemeral=True)
        guild = interaction.guild
        status = ticket["status"]
        if status != "open" and ticket["closed_by"]:
            status += f" (by <@{ticket['closed_by']}>)"
        extra = [p["user_id"] for p in await db.where("ticket_participants", ticket_id=ticket["id"])]
        transcripts_n = await _count(db, "SELECT COUNT(*) AS n FROM transcripts WHERE ticket_id = ?", (ticket["id"],))
        embed = discord.Embed(title=f"୨୧ Ticket #{ticket['number']:04d}", color=BLURPLE)
        embed.add_field(name="Status", value=status)
        embed.add_field(name="Owner", value=f"<@{ticket['owner_id']}>")
        embed.add_field(name="Category", value=truncate(category["name"], 100) if category else "—")
        embed.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "—")
        has_channel = ticket["status"] != "deleted" and _ticket_channel(guild, ticket) is not None
        embed.add_field(name="Channel", value=f"<#{ticket['channel_id']}>" if has_channel else "deleted")
        embed.add_field(name="Transcripts", value=f"{transcripts_n} saved")
        embed.add_field(name="Opened", value=_stamp(ticket["created_at"]), inline=False)
        if ticket["closed_at"]:
            embed.add_field(name="Closed", value=_stamp(ticket["closed_at"]), inline=False)
        if extra:
            embed.add_field(name="Added members", value=truncate(" ".join(f"<@{u}>" for u in extra), 1000),
                            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- ticket actions (same rules as the in-ticket buttons) -----------------
    @group.command(name="close", description="Close a ticket (this channel's ticket by default).")
    @app_commands.describe(number="Ticket number (leave empty inside a ticket channel)")
    async def close(interaction: discord.Interaction, number: Optional[int] = None):
        ctx = await find_ticket(interaction, number)
        if not ctx:
            return
        ticket, category, settings = ctx
        owner_ok = interaction.user.id == ticket["owner_id"] and (category is None or category["owner_can_close"])
        if not (owner_ok or is_staff(interaction.user, settings, category)):
            return await interaction.response.send_message(f"{ERR} You can't close this ticket.", ephemeral=True)
        tag = f"#{ticket['number']:04d}"
        if ticket["status"] != "open":
            return await interaction.response.send_message(
                f"{ERR} Ticket {tag} is already {ticket['status']}.", ephemeral=True)
        channel = _ticket_channel(interaction.guild, ticket)
        if channel is None:
            return await interaction.response.send_message(f"{ERR} I can't find that ticket's channel.", ephemeral=True)

        async def finish(i: discord.Interaction) -> str:
            await tickets.close_ticket(bot, channel, ticket["id"], i.user)
            now = await db.get("tickets", ticket["id"])
            return f"𐙚 Ticket {tag} closed." if now and now["status"] == "closed" else f"{ERR} Ticket {tag} was already closed."

        if settings["close_confirmation"]:
            async def yes(i: discord.Interaction):
                await i.response.edit_message(content="𐙚 Closing ticket…", view=None)
                await i.edit_original_response(content=await finish(i))
            return await interaction.response.send_message(
                f"Close ticket {tag}?", ephemeral=True,
                view=ConfirmView(interaction.user.id, yes, yes_label="Close ticket"))
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await finish(interaction), ephemeral=True)

    @group.command(name="reopen", description="Reopen a closed ticket (this channel's ticket by default).")
    @app_commands.describe(number="Ticket number (leave empty inside a ticket channel)")
    async def reopen(interaction: discord.Interaction, number: Optional[int] = None):
        ctx = await find_ticket(interaction, number)
        if not ctx:
            return
        ticket, category, settings = ctx
        if not is_staff(interaction.user, settings, category):
            return await interaction.response.send_message(f"{ERR} Only ticket staff can do that.", ephemeral=True)
        tag = f"#{ticket['number']:04d}"
        if ticket["status"] != "closed":
            return await interaction.response.send_message(f"{ERR} Ticket {tag} isn't closed.", ephemeral=True)
        channel = _ticket_channel(interaction.guild, ticket)
        if channel is None:
            return await interaction.response.send_message(f"{ERR} I can't find that ticket's channel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await strip_closed_buttons(bot, channel)
        await tickets.reopen_ticket(bot, channel, ticket, category, interaction.user)
        await interaction.followup.send(f"𐙚 Ticket {tag} reopened.", ephemeral=True)

    @group.command(name="delete", description="Delete a ticket channel for good. A transcript is saved first.")
    @app_commands.describe(number="Ticket number (leave empty inside a ticket channel)")
    async def delete(interaction: discord.Interaction, number: Optional[int] = None):
        ctx = await find_ticket(interaction, number)
        if not ctx:
            return
        ticket, category, settings = ctx
        if not is_staff(interaction.user, settings, category):
            return await interaction.response.send_message(f"{ERR} Only ticket staff can do that.", ephemeral=True)
        tag = f"#{ticket['number']:04d}"
        if ticket["status"] == "deleted":
            return await interaction.response.send_message(f"{ERR} Ticket {tag} is already deleted.", ephemeral=True)
        channel = _ticket_channel(interaction.guild, ticket)
        if channel is None:
            return await interaction.response.send_message(f"{ERR} I can't find that ticket's channel.", ephemeral=True)

        async def yes(i: discord.Interaction):
            await i.response.edit_message(content="⋆ Saving transcript and deleting channel…", view=None)
            await tickets.delete_ticket(bot, channel, ticket["id"], i.user, i)
            now = await db.get("tickets", ticket["id"])
            if now and now["status"] == "deleted":          # otherwise delete_ticket already explained the failure
                try:
                    await i.edit_original_response(content=f"{OK} Ticket {tag} deleted.")
                except discord.HTTPException:
                    pass

        await interaction.response.send_message(
            f"{ERR} Delete ticket {tag} permanently? A transcript is saved first.", ephemeral=True,
            view=ConfirmView(interaction.user.id, yes, yes_label="Delete ticket"))
