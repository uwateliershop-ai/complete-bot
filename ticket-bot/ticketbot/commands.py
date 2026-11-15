"""Slash commands under /ticket. Panel/ticket/settings commands live in admin.py and quick.py; deep configuration
lives in /ticket dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from . import admin, crud, images, post, quick, transcripts
from .util import BLURPLE, get_settings, is_admin, is_staff


async def admin_gate(interaction: discord.Interaction, bot) -> bool:
    settings = await get_settings(bot, interaction.guild_id)
    if is_admin(interaction.user, settings):
        return True
    await interaction.response.send_message(
        "⋆｡°✩ You need **Administrator** permission or a configured admin role to use this command.", ephemeral=True)
    return False

HELP_FIELDS = [
    ("⋆ 1 · Set up a panel",
     "`/ticket quickpanel` builds and posts a whole panel from one form. Options: `channel` · `banner` (picture links) "
     "· `thumbnail` · `staff_role` · `ticket_folder`.\n"
     "Run it again in the same channel to edit: the form comes back filled in."),
    ("⋆ The quickpanel form",
     "**Panel text** (plain message above the dropdown) · **Bottom bar** (optional small embed) · **Placeholder**\n"
     "**Categories**, one per line: `Label | description | symbol | question ; question+`\n"
     "Max 5 questions, `+` = long answer, none = no form.\n"
     "Example: `Order | Buy something | :server_emoji: | What are you buying? ; Details+`"),
    ("⋆ 2 · Pictures & GIFs (any number)",
     "`/ticket image add` · one per run: a `link` or an uploaded `file`, optional `position`\n"
     "`/ticket image bulk` · many at once, one link per line\n"
     "`/ticket image list` · `remove <number>` · `clear`\n"
     "`/ticket imageurl` · upload a picture, get a link for any link box\n"
     "`panel` can be left out in a quick panel's channel."),
    ("⋆ 3 · Plain messages, emojis, stickers",
     "`/ticket post` · the bot posts a normal message (no embed), with optional files.\n"
     "Type `:name:` for a server emoji and `{sticker:Name}` for a server sticker (max 3) in panel text, "
     "embeds, welcome messages and `/ticket post`. `:name:` also works in a button/option emoji box."),
    ("⋆ Manage panels",
     "`/ticket panels` · list panels · `/ticket publish` · post or re-post (old one replaced) · "
     "`/ticket unpublish` · take down, keep settings"),
    ("⋆ Manage tickets",
     "`/ticket list` · newest first, filter by `status` or `user`\n"
     "`/ticket info` · `close` · `reopen` · `delete` (staff): pass the ticket `number`, or leave it out inside the ticket\n"
     "`/ticket transcript [number]` (staff) · make one here, or fetch a saved one"),
    ("⋆ Settings",
     "`/ticket settings` · roles, channels and limits at a glance\n"
     "`/ticket dashboard` · change everything: panels, embeds, buttons, auto-responders, forms, automation, permissions, transcripts, general"),
    ("⋆ Cards, buttons & auto-responders",
     "Build them in `/ticket dashboard`: **Panels & Cards** post an embed with buttons. A button can open a ticket, "
     "open a link, send a reply (text and/or embed) or toggle a role.\n"
     "**Auto-Responders** answer keywords with text, an embed card and buttons, in tickets or anywhere in the server.\n"
     "**Automation** sets the staff nudge, idle reminder and auto-close.\n"
     "`/ticket autoreplies` · list your auto-responders"),
    ("⋆ Inside a ticket",
     "**Staff:** Claim · Unclaim · Transfer · Close · Transcript · Add/Remove user · Rename\n"
     "**Closed:** Reopen · Transcript · Delete"),
    ("⋆ Good to know",
     "Ticket names accept `{number}` `{username}` `{category}`; welcome messages `{user}` `{category}` `{number}`.\n"
     "Button colors: Discord only allows blurple, grey, green or red (`primary` · `secondary` · `success` · `danger`).\n"
     "No staff role set = only admins can see tickets."),
]


def build_group(bot) -> app_commands.Group:
    ticket = app_commands.Group(name="ticket", description="Ticket system", guild_only=True)

    @ticket.command(name="help", description="Admin guide to every ticket command.")
    async def help_command(interaction: discord.Interaction):
        if not await admin_gate(interaction, bot):
            return
        embed = discord.Embed(
            title="୨୧ Ticket Bot · Admin Help", color=BLURPLE,
            description="Members pick a **category** in the panel dropdown → fill its **form** (if any) → "
                        "get a private ticket channel with staff.\nAdmin only, unless marked (staff).")
        for name, value in HELP_FIELDS:
            embed.add_field(name=name, value=value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ticket.command(name="dashboard", description="Open the configuration dashboard.")
    async def dashboard(interaction: discord.Interaction):
        if await admin_gate(interaction, bot):
            screen = await crud.dashboard_screen(bot, interaction.user.id, interaction.guild_id)
            await interaction.response.send_message(ephemeral=True, **screen)

    quick.add_quick_command(bot, ticket, admin_gate)
    admin.add_admin_commands(bot, ticket, admin_gate)
    images.add_image_commands(bot, ticket, admin_gate)
    post.add_post_command(bot, ticket, admin_gate)
    post.add_imageurl_command(bot, ticket, admin_gate)

    @ticket.command(name="transcript", description="Generate a transcript here, or retrieve a saved one by number.")
    @app_commands.describe(number="Ticket number to retrieve (leave empty inside a ticket to generate a new one)")
    async def transcript(interaction: discord.Interaction, number: Optional[int] = None):
        settings = await get_settings(bot, interaction.guild_id)
        if number is None:
            ticket_row = await bot.db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
            if not ticket_row:
                return await interaction.response.send_message(
                    "Run this inside a ticket channel, or pass a ticket `number` to retrieve a saved transcript.",
                    ephemeral=True)
            category = await bot.db.get("categories", ticket_row["category_id"]) if ticket_row["category_id"] else None
            if not is_staff(interaction.user, settings, category):
                return await interaction.response.send_message("⋆｡°✩ Staff only.", ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            path = await transcripts.create_transcript(bot, interaction.channel, ticket_row, archive=True)
            return await interaction.followup.send("୨୧ Transcript generated.", file=discord.File(path), ephemeral=True)

        if not is_staff(interaction.user, settings):
            ticket_row = await bot.db.fetchone("SELECT * FROM tickets WHERE guild_id = ? AND number = ?",
                                               (interaction.guild_id, number))
            category = (await bot.db.get("categories", ticket_row["category_id"])
                        if ticket_row and ticket_row["category_id"] else None)
            if not is_staff(interaction.user, settings, category):
                return await interaction.response.send_message("⋆｡°✩ Staff only.", ephemeral=True)
        record = await transcripts.latest_transcript(bot, interaction.guild_id, number)
        if not record or not Path(record["file_path"]).exists():
            return await interaction.response.send_message(f"⋆｡°✩ No saved transcript for ticket #{number:04d}.",
                                                           ephemeral=True)
        await interaction.response.send_message(f"୨୧ Latest transcript for ticket #{number:04d}:",
                                                file=discord.File(record["file_path"]), ephemeral=True)

    return ticket
