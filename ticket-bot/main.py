"""Entry point:  python main.py"""
from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from ticketbot import orders, panels, tickets
from ticketbot.commands import build_group
from ticketbot.db import Database

load_dotenv()
log = logging.getLogger("ticketbot")


class TicketBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True            # privileged – enable "Server Members Intent" in the dev portal
        intents.message_content = True    # privileged – needed so transcripts contain message text
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.db = Database(os.getenv("DATABASE_PATH", "data/tickets.db"))
        self.user_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        await self.db.connect()
        # Persistent views: survive restarts because their custom_ids are stable.
        self.add_view(tickets.TicketControlView(self))
        self.add_view(tickets.ClosedView(self))
        await panels.register_panel_views(self)
        for view in orders.persistent_views(self):      # order flow buttons survive restarts
            self.add_view(view)
        self._watcher = asyncio.create_task(orders.watcher(self))

        self.tree.add_command(build_group(self))
        self.tree.add_command(orders.build_shop_group(self))
        self.tree.on_error = self.on_tree_error
        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        if dev_guild:                     # instant sync while developing
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Slash command error", exc_info=error)
        message = "❌ Something went wrong while running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def on_message(self, message: discord.Message) -> None:
        try:
            await orders.handle_message(self, message)   # pledge phrase, auto-replies, activity tracking
        except Exception:
            log.exception("on_message failed")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) – %d servers", self.user, self.user.id, len(self.guilds))

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Keep the database honest if someone deletes a ticket channel by hand."""
        ticket = await self.db.fetchone(
            "SELECT * FROM tickets WHERE channel_id = ? AND status != 'deleted'", (channel.id,))
        if ticket:
            await self.db.update("tickets", ticket["id"], {"status": "deleted"})
            await tickets.log_event(self, channel.guild, ticket, "ticket_deleted", None, "Channel deleted manually")

    async def close(self) -> None:
        watcher = getattr(self, "_watcher", None)
        if watcher:
            watcher.cancel()
        await self.db.close()
        await super().close()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    TicketBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
