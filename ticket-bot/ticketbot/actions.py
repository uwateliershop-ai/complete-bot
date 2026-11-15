"""What a configurable button does when clicked (shared by panel/card buttons and auto-responder buttons).

Actions:  create | form | category  -> open a ticket (see tickets.handle_entry)
          link                      -> opens a URL (Discord handles it, no callback)
          reply                     -> sends text and/or an embed template back (private by default)
          role                      -> gives the role, or takes it away when the member already has it
"""
from __future__ import annotations

import logging

import discord

from . import media, tickets
from .util import decorate, render, truncate

log = logging.getLogger(__name__)

STYLES = {
    "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
}
TICKET_ACTIONS = ("create", "form", "category")


def button_problem(data: dict) -> str | None:
    """Human readable reason why a button can't work yet (None = fine)."""
    action = data.get("action")
    if action in TICKET_ACTIONS and not data.get("category_id"):
        return "pick a **Ticket category**"
    if action == "link" and not data.get("url"):
        return "set the **Link URL** (Edit → Link & reply)"
    if action == "reply" and not (data.get("reply_text") or data.get("reply_embed_id")):
        return "add **Reply text** or pick a **Reply embed**"
    if action == "role" and not data.get("role_id"):
        return "pick the **Role to toggle**"
    return None


def button_kwargs(data: dict) -> dict:
    """label / emoji / style for a discord.ui.Button built from a button row."""
    label, emoji = decorate(data["label"], data.get("emoji"))
    return {"label": truncate(label, 80), "emoji": emoji, "style": STYLES.get(data["style"], discord.ButtonStyle.secondary)}


def is_link(data: dict) -> bool:
    return data.get("action") == "link" and bool(data.get("url"))


async def run(interaction: discord.Interaction, bot, data: dict, *, panel_id: int | None = None,
              context: dict | None = None) -> None:
    """Execute the action of a button row. `data` must be freshly loaded from the database."""
    problem = button_problem(data)
    if problem:
        return await interaction.response.send_message("⋆｡°✩ This button isn't set up yet.", ephemeral=True)
    action = data["action"]
    if action in TICKET_ACTIONS:
        return await tickets.handle_entry(interaction, bot, category_id=data["category_id"], action=action,
                                          form_id=data.get("form_id"), panel_id=panel_id)
    if action == "reply":
        return await send_reply(interaction, bot, data, context or {})
    if action == "role":
        return await toggle_role(interaction, data)
    await interaction.response.send_message("⋆｡°✩ This button has no action.", ephemeral=True)


def reply_values(interaction: discord.Interaction) -> dict:
    guild = interaction.guild
    return {"user": interaction.user.mention, "username": interaction.user.display_name,
            "server": guild.name if guild else "", "shop": guild.name if guild else "",
            "channel": getattr(interaction.channel, "mention", "")}


async def send_reply(interaction: discord.Interaction, bot, data: dict, context: dict) -> None:
    from . import panels                      # imported late: panels imports this module
    guild = interaction.guild
    text = render(data.get("reply_text") or "", **{**reply_values(interaction), **context})
    text = media.rich_text(guild, text).text
    embeds = []
    if data.get("reply_embed_id"):
        tpl = await bot.db.get("embed_templates", data["reply_embed_id"])
        if tpl:
            embeds.append(await panels.build_embed(bot, tpl))
    if not text and not embeds:
        return await interaction.response.send_message("⋆｡°✩ Nothing to show.", ephemeral=True)
    await interaction.response.send_message(
        content=truncate(text, 2000) or None, embeds=embeds, ephemeral=bool(data.get("ephemeral", 1)),
        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False))


async def toggle_role(interaction: discord.Interaction, data: dict) -> None:
    guild, member = interaction.guild, interaction.user
    role = guild.get_role(data["role_id"]) if guild else None
    if role is None:
        return await interaction.response.send_message("⋆｡°✩ That role no longer exists.", ephemeral=True)
    perms = getattr(role, "permissions", None)
    if (getattr(role, "managed", False) or (hasattr(role, "is_default") and role.is_default())
            or (perms is not None and getattr(perms, "administrator", False))):
        return await interaction.response.send_message("⋆｡°✩ That role can't be handed out with a button.", ephemeral=True)
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Role button")
            msg = f"ʚ♡ɞ Removed {role.mention}."
        else:
            await member.add_roles(role, reason="Role button")
            msg = f"ʚ♡ɞ You now have {role.mention}."
    except discord.Forbidden:
        return await interaction.response.send_message(
            "⋆｡°✩ I can't manage that role. Give me **Manage Roles** and move my role above it.", ephemeral=True)
    except discord.HTTPException:
        return await interaction.response.send_message("⋆｡°✩ Discord refused that change. Try again.", ephemeral=True)
    await interaction.response.send_message(msg, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
