"""Slash commands. Setup commands are top level (/dashboard /embed /quickpanel /boxpanel /panels /publish /image /post
/queue ...); ticket actions stay under /ticket (close, reopen, delete, info, list, transcript, settings, help).
Deep configuration lives in /dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from . import admin, crud, images, orderflow, post, quick, transcripts
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
     "`/quickpanel` builds and posts a whole panel from one form. `/boxpanel` does the same, but everything sits "
     "inside one box. Options: `channel` · `banner` (picture links) · `thumbnail` · `staff_role` · `ticket_folder`.\n"
     "Run it again in the same channel to edit: the form comes back filled in. Switch a panel between classic and box "
     "in `/dashboard` → Panels & Cards → Basics → Layout."),
    ("⋆ The panel form",
     "**Panel text** · **Bottom bar** (optional) · **Placeholder**\n"
     "**Categories**, one per line: `Label | description | symbol | question ; question+`\n"
     "Max 5 questions, `+` = long answer, none = no form.\n"
     "Example: `Order | Buy something | :server_emoji: | What are you buying? ; Details+`"),
    ("⋆ 2 · Pictures & GIFs (any number)",
     "`/image add` · one per run: a `link` or an uploaded `file`, optional `position`\n"
     "`/image bulk` · many at once, one link per line\n"
     "`/image list` · `remove <number>` · `clear`\n"
     "`/imageurl` · upload a picture, get a link for any link box"),
    ("⋆ 3 · Plain messages, emojis, stickers",
     "`/post` · the bot posts a normal message (no embed), with optional files.\n"
     "Type `:name:` for a server emoji (and `{sticker:Name}` for a sticker, max 3) in **every** text the bot sends: "
     "panels, embeds, welcome / close / claim messages, order and queue messages, vouches, transcripts, auto-replies, "
     "`/post`. `:name:` also works in button and option emoji boxes."),
    ("⋆ Manage panels",
     "`/dashboard` · everything · `/embed` · the embed builder · `/panels` · list panels · `/publish` · post or re-post · "
     "`/unpublish` · take down, keep settings"),
    ("⋆ Orders & queue",
     "Turn on **Order flow** for a category in `/dashboard` → Ticket Categories. The buyer confirms their order "
     "(before), gets a confirmation (after), staff move it through the queue.\n"
     "Edit all texts and stages in `/dashboard` → **Order Confirmation** and **Queue** (or `/queue settings`).\n"
     "`/queue board` · live board · `/queue list` · who is waiting"),
    ("⋆ Manage tickets",
     "`/ticket list` · newest first, filter by `status` or `user`\n"
     "`/ticket info` · `close` · `reopen` · `delete` (staff): pass the ticket `number`, or leave it out inside the ticket\n"
     "`/ticket transcript [number]` (staff) · make one here, or fetch a saved one\n"
     "`/ticket settings` · roles, channels and limits at a glance"),
    ("⋆ Everything editable in Discord",
     "`/dashboard` → Panels & Cards · Embed Templates · Buttons · Dropdowns · Auto-Responders · Forms · Automation · "
     "**Messages** · **Order Confirmation** · **Queue** · Vouch Settings · Transcript Settings · Permissions · General."),
    ("⋆ Inside a ticket",
     "**Staff:** Claim · Unclaim · Transfer · Close · Transcript · Add/Remove user · Rename\n"
     "**Closed:** Reopen · Transcript · Delete"),
    ("⋆ Good to know",
     "Ticket names accept `{number}` `{username}` `{category}`; welcome messages `{user}` `{category}` `{number}`.\n"
     "Button colors: Discord only allows blurple, grey, green or red (`primary` · `secondary` · `success` · `danger`).\n"
     "No staff role set = only admins can see tickets."),
]


def build_group(bot) -> app_commands.Group:
    """Registers the top-level setup commands on the tree and returns the /ticket group (not yet added)."""
    tree = bot.tree
    ticket = app_commands.Group(name="ticket", description="Ticket actions", guild_only=True)

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

    @tree.command(name="dashboard", description="Open the configuration dashboard.")
    @app_commands.guild_only()
    async def dashboard(interaction: discord.Interaction):
        if await admin_gate(interaction, bot):
            screen = await crud.dashboard_screen(bot, interaction.user.id, interaction.guild_id)
            await interaction.response.send_message(ephemeral=True, **screen)

    @tree.command(name="embed", description="Open the embed builder (create and edit embeds).")
    @app_commands.guild_only()
    async def embed_builder(interaction: discord.Interaction):
        if await admin_gate(interaction, bot):
            screen = await crud.section_screen(bot, interaction.user.id, interaction.guild_id, "embeds")
            await interaction.response.send_message(ephemeral=True, **screen)

    quick.add_quick_command(bot, tree, admin_gate)
    admin.add_admin_commands(bot, ticket, admin_gate, root=tree)
    images.add_image_commands(bot, tree, admin_gate)
    post.add_post_command(bot, tree, admin_gate)
    post.add_imageurl_command(bot, tree, admin_gate)
    orderflow.add_queue_commands(bot, tree, admin_gate)

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

    for cmd in tree.get_commands():                      # every command works in servers only
        cmd.guild_only = True
    return ticket
