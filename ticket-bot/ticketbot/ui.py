"""Shared view plumbing: admin-gated base view, confirm dialog, show()."""
from __future__ import annotations

import logging

import discord

from .util import decorate, get_settings, is_admin

log = logging.getLogger(__name__)


async def show(interaction: discord.Interaction, *, content=None, embeds=None, view=None) -> None:
    """Render a screen: edit the current message if there is one, otherwise send an ephemeral."""
    kwargs = {"content": content, "embeds": embeds or [], "view": view}
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    elif interaction.message is not None:
        await interaction.response.edit_message(**kwargs)
    else:
        if view is None:
            kwargs.pop("view")
        await interaction.response.send_message(ephemeral=True, **kwargs)


class BaseView(discord.ui.View):
    """Dashboard view: only the invoking administrator may use it."""

    def __init__(self, bot, user_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = user_id

    def btn(self, label, callback, *, style=discord.ButtonStyle.secondary, emoji=None,
            row=None, disabled=False) -> discord.ui.Button:
        label, emoji = decorate(label, emoji)
        button = discord.ui.Button(label=label, style=style, emoji=emoji, row=row, disabled=disabled)
        button.callback = callback
        self.add_item(button)
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        settings = await get_settings(self.bot, interaction.guild_id)
        if not is_admin(interaction.user, settings):
            await interaction.response.send_message("⋆｡°✩ Administrator access required.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item) -> None:
        log.exception("Dashboard error", exc_info=error)
        msg = "⋆｡°✩ Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class ConfirmView(discord.ui.View):
    """Yes / No prompt used before every destructive operation."""

    def __init__(self, user_id: int, on_yes, on_no=None, *, yes_label="Confirm"):
        super().__init__(timeout=120)
        self.user_id, self.on_yes, self.on_no = user_id, on_yes, on_no
        self.yes.label = yes_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This prompt isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ʚ♡ɞ Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.on_yes(interaction)

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        if self.on_no:
            await self.on_no(interaction)
        else:
            await interaction.response.edit_message(content="Cancelled.", embeds=[], view=None)
