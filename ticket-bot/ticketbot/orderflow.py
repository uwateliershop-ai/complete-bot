"""Order confirmation + queue.

Turn it on per category: /dashboard → Category Management → "Order flow".

    ticket opens → form → [ confirm your order ]  (Yes, proceed / Edit order)         <- "before"
                        → [ order confirmed ]  + queue message for staff (Processing / Done)   <- "after"
                        → status updates in the ticket (+ DM) → live queue board (optional)

Every text, button label and stage name is edited in /dashboard → Order Confirmation and → Queue."""
from __future__ import annotations

import json
import logging
from typing import Optional

import discord
from discord import app_commands

from . import texts
from .util import BLURPLE, GREEN, get_settings, is_staff, jload, truncate

log = logging.getLogger(__name__)

PINK = 0xFFB7D5
STATUS_COLOR = {"pending": PINK, "queued": PINK, "processing": BLURPLE, "done": GREEN}
ACTIVE = ("queued", "processing")


def now_iso() -> str:
    return discord.utils.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------
async def order_by_channel(bot, channel_id: int) -> dict | None:
    return await bot.db.fetchone(
        "SELECT o.* FROM ticket_orders o JOIN tickets t ON t.id = o.ticket_id WHERE t.channel_id = ?", (channel_id,))


async def order_by_queue_message(bot, message_id: int) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM ticket_orders WHERE queue_message_id = ?", (message_id,))


async def position(bot, order: dict) -> int:
    row = await bot.db.fetchone(
        "SELECT COUNT(*) AS n FROM ticket_orders WHERE guild_id = ? AND status IN ('queued', 'processing') AND id <= ?",
        (order["guild_id"], order["id"]))
    return max(1, row["n"])


def stage(guild, settings, status: str) -> str:
    return texts.say(guild, settings, f"stage_{'queued' if status == 'pending' else status}")


def _values(guild, settings, order: dict, ticket: dict, category: dict | None, pos: int | None = None) -> dict:
    return {
        "user": f"<@{order['owner_id']}>", "code": order["code"], "position": pos if pos is not None else "–",
        "category": category["name"] if category else "—", "ticket": f"<#{ticket['channel_id']}>",
        "number": f"{ticket['number']:04d}", "status": stage(guild, settings, order["status"]),
        "staff": f"<@{order['handled_by']}>" if order["handled_by"] else "—",
        "yes": texts.say(guild, settings, "order_yes_label"), "edit": texts.say(guild, settings, "order_edit_label"),
    }


def _add_answers(embed: discord.Embed, answers: list[dict]) -> None:
    for ans in answers[:10]:
        embed.add_field(name=truncate(ans["q"], 256), value=truncate(ans["a"], 1024), inline=False)


# ---------------------------------------------------------------------------
# "before": confirm the order
# ---------------------------------------------------------------------------
def confirm_embed(guild, settings, order, ticket, category, answers) -> discord.Embed:
    v = _values(guild, settings, order, ticket, category)
    embed = discord.Embed(title=truncate(texts.say(guild, settings, "order_before_title", **v), 256), color=PINK,
                          description=truncate(texts.say(guild, settings, "order_before_text", **v), 4000))
    _add_answers(embed, answers)
    embed.set_footer(text=f"Order {order['code']}")
    return embed


class OrderConfirmView(discord.ui.View):
    """Persistent. Labels come from the settings when the message is sent."""

    def __init__(self, bot, settings: dict | None = None, guild=None):
        super().__init__(timeout=None)
        self.bot = bot
        if settings is not None:
            self.yes.label = truncate(texts.say(guild, settings, "order_yes_label"), 80)
            self.edit.label = truncate(texts.say(guild, settings, "order_edit_label"), 80)

    async def _order(self, interaction: discord.Interaction):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order:
            await interaction.response.send_message("⋆｡°✩ This order was not found.", ephemeral=True)
            return None, None
        if order["status"] != "pending":
            await interaction.response.send_message("⋆｡°✩ This order is already confirmed ♡", ephemeral=True)
            return None, None
        ticket = await self.bot.db.get("tickets", order["ticket_id"])
        category = await self.bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        settings = await get_settings(self.bot, interaction.guild_id)
        if interaction.user.id != order["owner_id"] and not is_staff(interaction.user, settings, category):
            await interaction.response.send_message("⋆｡°✩ Only the buyer can confirm or edit this order.", ephemeral=True)
            return None, None
        return order, ticket

    @discord.ui.button(label="Yes, proceed", style=discord.ButtonStyle.success, custom_id="tb:o:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        order, ticket = await self._order(interaction)
        if order:
            await interaction.response.defer()
            await confirm(self.bot, interaction.guild, interaction.channel, order, ticket)

    @discord.ui.button(label="Edit order", style=discord.ButtonStyle.secondary, custom_id="tb:o:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        order, ticket = await self._order(interaction)
        if not order:
            return
        questions = []
        if ticket["category_id"]:
            category = await self.bot.db.get("categories", ticket["category_id"])
            if category and category["form_id"]:
                questions = await self.bot.db.where("form_questions", order="position, id", form_id=category["form_id"])
        await interaction.response.send_modal(EditOrderModal(self.bot, order, ticket, questions))


class EditOrderModal(discord.ui.Modal, title="Edit your order"):
    def __init__(self, bot, order: dict, ticket: dict, questions: list[dict]):
        super().__init__()
        self.bot, self.order_id = bot, order["id"]
        old = {a["q"]: a["a"] for a in jload(ticket["form_answers"])}
        self.inputs: list[tuple[str, discord.ui.TextInput]] = []
        for q in (questions[:5] or [{"label": "Order details", "style": "paragraph", "required": 1}]):
            paragraph = q["style"] == "paragraph"
            label = q["label"]
            current = old.get(label)
            ti = discord.ui.TextInput(
                label=truncate(label, 45), style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                required=bool(q["required"]), max_length=1000 if paragraph else 300,
                default=None if current in (None, "—") else current)
            self.add_item(ti)
            self.inputs.append((label, ti))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        order = await bot.db.get("ticket_orders", self.order_id)
        if not order or order["status"] != "pending":
            return await interaction.response.send_message("⋆｡°✩ This order is already confirmed ♡", ephemeral=True)
        await interaction.response.defer()
        answers = [{"q": label, "a": ti.value or "—"} for label, ti in self.inputs]
        await bot.db.update("tickets", order["ticket_id"], {"form_answers": json.dumps(answers)})
        ticket = await bot.db.get("tickets", order["ticket_id"])
        category = await bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        settings = await get_settings(bot, interaction.guild_id)
        guild, channel = interaction.guild, interaction.channel

        from .tickets import form_embed
        try:
            if order["form_message_id"]:
                await channel.get_partial_message(order["form_message_id"]).edit(embed=form_embed(guild, settings, answers))
            else:
                msg = await channel.send(embed=form_embed(guild, settings, answers))
                await bot.db.update("ticket_orders", order["id"], {"form_message_id": msg.id})
        except discord.HTTPException:
            log.warning("Could not update the form message of order %s", order["code"])
        try:
            await channel.get_partial_message(order["confirm_message_id"]).edit(
                embed=confirm_embed(guild, settings, order, ticket, category, answers))
        except discord.HTTPException:
            pass
        await interaction.followup.send("ʚ♡ɞ Order updated.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Edit order error", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("⋆｡°✩ Something went wrong.", ephemeral=True)


async def start(bot, channel: discord.TextChannel, ticket: dict, category: dict, answers: list[dict],
                form_message_id: int | None, settings: dict) -> None:
    """Called right after a ticket is created. Does nothing unless the category has Order flow turned on."""
    if not category.get("is_order"):
        return
    guild = channel.guild
    order_id = await bot.db.insert("ticket_orders", {
        "guild_id": guild.id, "ticket_id": ticket["id"], "code": f"{ticket['number']:04d}",
        "owner_id": ticket["owner_id"], "status": "pending", "form_message_id": form_message_id,
        "created_at": now_iso()})
    order = await bot.db.get("ticket_orders", order_id)
    msg = await channel.send(content=f"<@{ticket['owner_id']}>",
                             embed=confirm_embed(guild, settings, order, ticket, category, answers),
                             view=OrderConfirmView(bot, settings, guild),
                             allowed_mentions=discord.AllowedMentions(users=True))
    await bot.db.update("ticket_orders", order_id, {"confirm_message_id": msg.id})


# ---------------------------------------------------------------------------
# "after": confirmed → queue
# ---------------------------------------------------------------------------
def _locked(label: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True))
    return view


def queue_embed(guild, settings, order, ticket, category, answers, pos=None) -> discord.Embed:
    v = _values(guild, settings, order, ticket, category, pos)
    embed = discord.Embed(title=truncate(texts.say(guild, settings, "queue_title", **v), 256),
                          description=truncate(texts.say(guild, settings, "queue_text", **v), 4000),
                          color=STATUS_COLOR.get(order["status"], PINK), timestamp=discord.utils.utcnow())
    _add_answers(embed, answers)
    embed.add_field(name="Ticket", value=v["ticket"], inline=True)
    embed.add_field(name="Handled by", value=v["staff"], inline=True)
    embed.set_footer(text=f"Order {order['code']}")
    return embed


class QueueView(discord.ui.View):
    """Persistent staff buttons on the queue message. Labels come from the settings when the message is sent."""

    def __init__(self, bot, settings: dict | None = None, guild=None):
        super().__init__(timeout=None)
        self.bot = bot
        if settings is not None:
            self.processing.label = truncate(texts.say(guild, settings, "queue_btn_processing"), 80)
            self.done.label = truncate(texts.say(guild, settings, "queue_btn_done"), 80)

    async def _step(self, interaction: discord.Interaction, status: str, allowed: tuple[str, ...]) -> None:
        order = await order_by_queue_message(self.bot, interaction.message.id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ Order not found for this message.", ephemeral=True)
        ticket = await self.bot.db.get("tickets", order["ticket_id"])
        category = await self.bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        settings = await get_settings(self.bot, interaction.guild_id)
        if not is_staff(interaction.user, settings, category):
            return await interaction.response.send_message("⋆｡°✩ Only staff can do that.", ephemeral=True)
        if order["status"] not in allowed:
            return await interaction.response.send_message(
                f"⋆｡°✩ This order is currently **{stage(interaction.guild, settings, order['status'])}**.", ephemeral=True)
        await interaction.response.defer()
        await set_status(self.bot, interaction.guild, order, status, interaction.user)

    @discord.ui.button(label="Processing", style=discord.ButtonStyle.primary, custom_id="tb:o:proc")
    async def processing(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._step(interaction, "processing", ("queued",))

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success, custom_id="tb:o:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._step(interaction, "done", ("queued", "processing"))


async def confirm(bot, guild: discord.Guild, channel: discord.TextChannel, order: dict, ticket: dict) -> None:
    settings = await get_settings(bot, guild.id)
    category = await bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
    answers = jload(ticket["form_answers"])
    await bot.db.update("ticket_orders", order["id"], {"status": "queued", "confirmed_at": now_iso()})
    order = await bot.db.get("ticket_orders", order["id"])
    pos = await position(bot, order)

    try:                                                         # lock the "before" message
        await channel.get_partial_message(order["confirm_message_id"]).edit(view=_locked("ʚ♡ɞ Confirmed"))
    except discord.HTTPException:
        pass

    # staff queue message (queue channel, or inside the ticket when no queue channel is set)
    target = guild.get_channel(settings.get("queue_channel_id") or 0)
    if not isinstance(target, discord.abc.Messageable):
        target = channel
    qmsg = await target.send(embed=queue_embed(guild, settings, order, ticket, category, answers, pos),
                             view=QueueView(bot, settings, guild))
    await bot.db.update("ticket_orders", order["id"], {"queue_channel_id": target.id, "queue_message_id": qmsg.id})

    # "after" message for the buyer
    v = _values(guild, settings, order, ticket, category, pos)
    await channel.send(content=f"<@{order['owner_id']}>", embed=discord.Embed(
        title=truncate(texts.say(guild, settings, "order_after_title", **v), 256), color=GREEN,
        description=truncate(texts.say(guild, settings, "order_after_text", **v), 4000)),
        allowed_mentions=discord.AllowedMentions(users=True))
    from .tickets import log_event
    await log_event(bot, guild, ticket, "order_confirmed", order["owner_id"], f"Order {order['code']}")
    await refresh_board(bot, guild)


async def set_status(bot, guild: discord.Guild, order: dict, status: str, actor) -> None:
    changes = {"status": status}
    if status == "processing":
        changes["handled_by"] = actor.id
    if status == "done":
        changes["done_at"] = now_iso()
        changes["handled_by"] = order["handled_by"] or actor.id
    await bot.db.update("ticket_orders", order["id"], changes)
    order = await bot.db.get("ticket_orders", order["id"])
    settings = await get_settings(bot, guild.id)
    ticket = await bot.db.get("tickets", order["ticket_id"])
    category = await bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
    answers = jload(ticket["form_answers"])

    qch = guild.get_channel(order["queue_channel_id"] or 0)
    if qch is not None and order["queue_message_id"]:
        try:
            await qch.get_partial_message(order["queue_message_id"]).edit(
                embed=queue_embed(guild, settings, order, ticket, category, answers, await position(bot, order)),
                view=None if status == "done" else QueueView(bot, settings, guild))
        except discord.HTTPException:
            log.warning("Could not update the queue message of order %s", order["code"])

    v = _values(guild, settings, order, ticket, category)
    text = texts.say(guild, settings, "queue_update_text", **v)
    channel = guild.get_channel(ticket["channel_id"])
    if channel is not None:
        try:
            await channel.send(content=f"<@{order['owner_id']}>", allowed_mentions=discord.AllowedMentions(users=True),
                               embed=discord.Embed(description=truncate(text, 4000), color=STATUS_COLOR[status]))
        except discord.HTTPException:
            pass
    owner = guild.get_member(order["owner_id"])
    if owner and settings.get("queue_dm"):
        try:
            await owner.send(embed=discord.Embed(description=truncate(text, 4000), color=STATUS_COLOR[status]))
        except (discord.Forbidden, discord.HTTPException):
            pass
    from .tickets import log_event
    await log_event(bot, guild, ticket, f"order_{status}", getattr(actor, "id", None), f"Order {order['code']}")
    await refresh_board(bot, guild)


# ---------------------------------------------------------------------------
# live board
# ---------------------------------------------------------------------------
async def board_embed(bot, guild, settings) -> discord.Embed:
    rows = await bot.db.fetchall(
        "SELECT o.* FROM ticket_orders o JOIN tickets t ON t.id = o.ticket_id "
        "WHERE o.guild_id = ? AND o.status IN ('queued', 'processing') AND t.status != 'deleted' ORDER BY o.id LIMIT 25",
        (guild.id,))
    lines = [f"**{i}.** `{o['code']}` · <@{o['owner_id']}> · {stage(guild, settings, o['status'])}"
             for i, o in enumerate(rows, start=1)]
    embed = discord.Embed(title=truncate(texts.say(guild, settings, "board_title"), 256), color=PINK,
                          timestamp=discord.utils.utcnow(),
                          description=truncate(texts.say(guild, settings, "board_text") + "\n\n"
                                               + ("\n".join(lines) or "*The queue is empty.*"), 4000))
    return embed


async def refresh_board(bot, guild: discord.Guild) -> None:
    settings = await get_settings(bot, guild.id)
    channel = guild.get_channel(settings.get("board_channel_id") or 0)
    if channel is None or not settings.get("board_message_id"):
        return
    try:
        await channel.get_partial_message(settings["board_message_id"]).edit(embed=await board_embed(bot, guild, settings))
    except discord.NotFound:
        await bot.db.update("guild_settings", guild.id, {"board_message_id": None})
    except discord.HTTPException:
        pass


async def post_board(bot, guild: discord.Guild, channel: discord.TextChannel) -> str:
    settings = await get_settings(bot, guild.id)
    old = guild.get_channel(settings.get("board_channel_id") or 0)
    msg = await channel.send(embed=await board_embed(bot, guild, settings))
    if old is not None and settings.get("board_message_id"):
        try:
            await old.get_partial_message(settings["board_message_id"]).delete()
        except discord.HTTPException:
            pass
    await bot.db.update("guild_settings", guild.id, {"board_channel_id": channel.id, "board_message_id": msg.id})
    return f"ʚ♡ɞ Queue board posted in {channel.mention}. It updates by itself."


# ---------------------------------------------------------------------------
# /queue
# ---------------------------------------------------------------------------
def add_queue_commands(bot, tree, admin_gate) -> None:
    queue = app_commands.Group(name="queue", description="Order queue", guild_only=True)

    @queue.command(name="board", description="Post a live queue board that updates by itself.")
    @app_commands.describe(channel="Where to post it (default: this channel)")
    async def board(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await admin_gate(interaction, bot):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await post_board(bot, interaction.guild, channel or interaction.channel)
        await interaction.followup.send(text, ephemeral=True)

    @queue.command(name="list", description="Show the orders that are waiting or being worked on.")
    async def list_orders(interaction: discord.Interaction):
        settings = await get_settings(bot, interaction.guild_id)
        if not is_staff(interaction.user, settings):
            return await interaction.response.send_message("⋆｡°✩ Staff only.", ephemeral=True)
        await interaction.response.send_message(
            embed=await board_embed(bot, interaction.guild, settings), ephemeral=True)

    @queue.command(name="settings", description="Edit queue texts, stages and channel.")
    async def queue_settings(interaction: discord.Interaction):
        if not await admin_gate(interaction, bot):
            return
        from . import crud
        screen = await crud.section_screen(bot, interaction.user.id, interaction.guild_id, "queue")
        await interaction.response.send_message(ephemeral=True, **screen)

    tree.add_command(queue)
