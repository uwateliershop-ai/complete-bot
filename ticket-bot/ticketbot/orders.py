"""Shop layer on top of the ticket system.

Order flow inside an "order" ticket:
    pledge (type "i swear")  ->  order form  ->  order confirmation  ->  payment method  ->  "payment sent"
    ->  staff confirms payment (+ price, + deal slot)  ->  lined up  ->  processing  ->  donesies  ->  vouch

Also here: live deals/slots panel, keyword auto-responder, unclaimed / idle ticket nudges, and the /shop admin
command group (everything editable from Discord, no code edits).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands

from . import media
from .util import DARK, get_settings, is_admin, is_staff, jload, render, truncate

log = logging.getLogger(__name__)

PINK = 0xF8BBD9
STAGES = ("lined_up", "processing", "done")
STAGE_LABEL = {"lined_up": "lined up", "processing": "processing", "done": "donesies"}
STATUS_WORD = {"lined_up": "noted", "processing": "processing", "done": "donesies"}
BARS = {"lined_up": "ε ─◉────✿────✿─ з", "processing": "ε ─✿────◉────✿─ з", "done": "ε ─✿────✿────◉─ з"}
METHODS = ("gcash", "paypal", "robux")
ACTIVE_QUEUE = ("paid_review", "lined_up", "processing")     # staff are on it: don't nag the buyer
REPLY_COOLDOWN = 300                                         # seconds, per ticket per auto-reply
_reply_seen: dict[tuple[int, int], float] = {}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return discord.utils.utcnow().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rt(guild, text: str) -> str:
    """:server_emoji: tokens -> real emoji."""
    return media.rich_text(guild, text).text


def fmt_price(value) -> str:
    return "₱—" if value in (None, "") else f"₱{float(value):,.2f}"


def parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if 0 <= value < 10_000_000 else None


def normalize_method(text: str) -> str | None:
    t = (text or "").lower()
    for m in METHODS:
        if m in t or (m == "gcash" and "gcsh" in t) or (m == "robux" and "robx" in t):
            return m
    return None


def progress_text(status: str) -> str:
    """Bar + stage labels: reached stages bold, upcoming ones struck through."""
    if status not in STAGES:
        status = "lined_up"
    cur = STAGES.index(status)
    labels = [f"**{STAGE_LABEL[s]}**" if i <= cur else f"~~{STAGE_LABEL[s]}~~" for i, s in enumerate(STAGES)]
    return f"{BARS[status]}\n" + "   ".join(labels)


def reply_matches(row: dict, text: str) -> bool:
    t = text.lower().strip()
    keys = [k.strip().lower() for k in row["trigger"].split(",") if k.strip()]
    if not keys:
        return False
    if row["match_type"] == "exact":
        return t in keys
    return any(re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", t) for k in keys)


async def get_shop(bot, guild_id: int) -> dict:
    await bot.db.execute("INSERT OR IGNORE INTO shop_settings (guild_id) VALUES (?)", (guild_id,))
    return await bot.db.fetchone("SELECT * FROM shop_settings WHERE guild_id = ?", (guild_id,))


async def shop_update(bot, guild_id: int, **values) -> None:
    if not values:
        return
    await get_shop(bot, guild_id)
    sets = ", ".join(f"{k} = ?" for k in values)          # column names come from code only
    await bot.db.execute(f"UPDATE shop_settings SET {sets} WHERE guild_id = ?", (*values.values(), guild_id))


async def order_by_channel(bot, channel_id: int) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM orders WHERE channel_id = ? ORDER BY id DESC LIMIT 1", (channel_id,))


async def order_by_queue_message(bot, message_id: int) -> dict | None:
    return await bot.db.fetchone("SELECT * FROM orders WHERE queue_message_id = ?", (message_id,))


async def update_order(bot, order_id: int, **values) -> dict:
    await bot.db.update("orders", order_id, {**values, "updated_at": now_iso()})
    return await bot.db.get("orders", order_id)


def is_order_category(shop: dict, category: dict | None) -> bool:
    if not category:
        return False
    if shop["order_category_id"]:
        return category["id"] == shop["order_category_id"]
    return "order" in category["name"].lower()


async def staff_mentions(bot, guild: discord.Guild, ticket: dict) -> str:
    if ticket.get("claimed_by"):
        return f"<@{ticket['claimed_by']}>"
    settings = await get_settings(bot, guild.id)
    category = await bot.db.get("categories", ticket["category_id"]) if ticket.get("category_id") else None
    ids = set(jload(settings["staff_role_ids"])) | set(jload(category["staff_role_ids"]) if category else [])
    return " ".join(r.mention for r in (guild.get_role(i) for i in ids) if r)


async def staff_check(interaction: discord.Interaction, bot, ticket: dict | None) -> bool:
    settings = await get_settings(bot, interaction.guild_id)
    category = await bot.db.get("categories", ticket["category_id"]) if ticket and ticket.get("category_id") else None
    if is_staff(interaction.user, settings, category):
        return True
    await interaction.response.send_message("⋆｡°✩ only ticket staff can use that button.", ephemeral=True)
    return False


async def owner_check(interaction: discord.Interaction, order: dict) -> bool:
    if interaction.user.id == order["owner_id"]:
        return True
    await interaction.response.send_message("⋆｡°✩ this is for the ticket owner only.", ephemeral=True)
    return False


async def safe_dm(guild: discord.Guild, user_id: int, **kwargs) -> None:
    member = guild.get_member(user_id)
    if not member:
        return
    try:
        await member.send(**kwargs)
    except discord.HTTPException:
        pass                                              # DMs closed: the ticket message still tells them


def partial(guild: discord.Guild, channel_id: int | None, message_id: int | None):
    ch = guild.get_channel(channel_id or 0)
    if ch is None or not message_id or not hasattr(ch, "get_partial_message"):
        return None
    return ch.get_partial_message(message_id)


def locked_view(label: str = "no touchie!") -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True))
    return view


# ---------------------------------------------------------------------------
# embeds
# ---------------------------------------------------------------------------
def pledge_embed(guild, shop: dict) -> discord.Embed:
    rules = f"<#{shop['rules_channel_id']}>" if shop["rules_channel_id"] else "my server rules & guidelines"
    name = shop["shop_name"]
    desc = (f"✿ **{name}'s pledge** ✿\n\n"
            f"• purchasing from me means\n• you solemnly pledge that **you**\n• have read & agreed to {rules} !\n"
            f"• you may proceed by typing `{shop['pledge_phrase']}`")
    return discord.Embed(description=rt(guild, desc), color=PINK)


def form_embed(guild, order: dict) -> discord.Embed:
    lines = [f"♡ : __username__\n╰ {order['username'] or '—'}",
             f"♡ : __order__\n╰ {order['item'] or '—'}",
             f"♡ : __quantity__\n╰ {order['quantity']}",
             f"♡ : __mode of payment__\n╰ {order['pay_method'] or '—'}"]
    return discord.Embed(description="\n".join(lines), color=PINK)


def confirm_embed(guild, order: dict) -> discord.Embed:
    method = order["pay_method"] or "—"
    desc = (f"୨୧  **Order Confirmation**\n\n"
            f"[ {order['quantity']} ] ♡ **{order['item']}**\n"
            f"{fmt_price(order['price'])} ◦ __{method}__\n"
            f"♡ **notes :** {order['notes'] or 'none'}")
    return discord.Embed(description=desc, color=PINK)


def payment_embed(guild, shop: dict, order: dict) -> discord.Embed:
    desc = ("order has been **confirmed** !\nkindly choose ur **mode** of payment below and patiently wait "
            "for your ticket handler to respond ♡\n\n"
            "• send : **gcash** for my gcash payment\n• send : **paypal** for my paypal payment\n"
            f"• send : **robux** for my robux payment\n\n📌 **note :** {shop['robux_note']}\n\n"
            "once you've paid, press **payment sent**.")
    return discord.Embed(description=rt(guild, desc), color=PINK)


def queue_text(order: dict, deal: dict | None, status: str) -> str:
    title = deal["name"] if deal else "order"
    return (f"‿ ‿ ♡ ‿ ‿\n**{title}**\n"
            f"<#{order['channel_id']}>  <@{order['owner_id']}>\n"
            f"[ {order['quantity']} ] {order['item']}\n"
            f"`{fmt_price(order['price'])}` — {order['pay_method'] or '—'}\n"
            f"♡ **status :** __{STATUS_WORD.get(status, status)}__\n‿ ‿ ♡ ‿ ‿")


async def queue_embed(bot, order: dict) -> discord.Embed:
    deal = await bot.db.get("deals", order["deal_id"]) if order["deal_id"] else None
    return discord.Embed(description=queue_text(order, deal, order["status"]), color=PINK)


async def ticket_status_embed(bot, guild: discord.Guild, order: dict) -> discord.Embed:
    shop = await get_shop(bot, guild.id)
    track = f"<#{order['queue_channel_id']}>" if order["queue_channel_id"] else "this ticket"
    updates = f"<#{shop['updates_channel_id']}>" if shop["updates_channel_id"] else "our updates channel"
    desc = (f"✿ **order is {STAGE_LABEL.get(order['status'], order['status'])}** ·  `{order['code']}`\n\n"
            f"track your ordie status at : {track}\ncheck {updates} for updaties\n\n"
            "expect the unexpected ! delays may happen on our end, upds are given :3\n"
            "only ping if necessary ♡\n\n"
            f"{progress_text(order['status'])}\n\n"
            f"tenchu for ur patience <@{order['owner_id']}>")
    return discord.Embed(description=rt(guild, desc), color=PINK)


def deals_embed(guild, deals: list[dict]) -> discord.Embed:
    parts = []
    for d in deals:
        block = f"✿ **{d['name']}** : {d['price'] or '—'}\n♡ inclusions :\n{d['inclusions'] or '—'}"
        parts.append(block)
    slots = [f"`{i:02d}` : {d['slots_used']}/{d['slots_max']}  {d['name']}"
             for i, d in enumerate(deals, 1) if d["slots_max"]]
    desc = "\n\n".join(parts) or "no deals right now ♡"
    if slots:
        desc += "\n\n**slots !**\n" + "\n".join(slots)
    return discord.Embed(description=rt(guild, truncate(desc, 4000)), color=PINK)


# ---------------------------------------------------------------------------
# flow: start / pledge
# ---------------------------------------------------------------------------
async def on_ticket_created(bot, channel: discord.TextChannel, ticket: dict, category: dict | None,
                            user: discord.Member, answers: list[dict]) -> None:
    """Called by tickets.create_ticket. Starts the order flow when this is an order ticket."""
    guild = channel.guild
    shop = await get_shop(bot, guild.id)
    if not is_order_category(shop, category):
        return
    await bot.db.execute("UPDATE shop_settings SET order_counter = order_counter + 1 WHERE guild_id = ?", (guild.id,))
    number = (await get_shop(bot, guild.id))["order_counter"]
    order_id = await bot.db.insert("orders", {
        "guild_id": guild.id, "ticket_id": ticket["id"], "channel_id": channel.id, "owner_id": user.id,
        "code": f"UW-{number:04d}", "status": "pledge", "created_at": now_iso(),
    })
    msg = await channel.send(content=f"ʚ♡ɞ {user.mention}'s ordie !", embed=pledge_embed(guild, shop))
    try:
        await msg.pin()
    except discord.HTTPException:
        pass
    await update_order(bot, order_id, pledge_message_id=msg.id)


async def accept_pledge(bot, message: discord.Message, order: dict) -> None:
    guild = message.guild
    await update_order(bot, order["id"], status="form")
    embed = discord.Embed(description="ʚ♡ɞ **pledge accepted** !\nplease fill out the order form below ♡", color=PINK)
    await message.channel.send(content=message.author.mention, embed=embed, view=OrderFormView(bot))


# ---------------------------------------------------------------------------
# flow: order form -> confirmation
# ---------------------------------------------------------------------------
class OrderModal(discord.ui.Modal, title="order form"):
    def __init__(self, bot, order: dict):
        super().__init__()
        self.bot, self.order_id = bot, order["id"]
        self.username = discord.ui.TextInput(label="username", placeholder="@yourname", max_length=60,
                                             default=order["username"] or None)
        self.item = discord.ui.TextInput(label="order", placeholder="e.g. bot package", max_length=100,
                                         default=order["item"] or None)
        self.quantity = discord.ui.TextInput(label="quantity", placeholder="1", max_length=4,
                                             default=str(order["quantity"] or 1))
        self.method = discord.ui.TextInput(label="mode of payment", placeholder="gcash / paypal / robux",
                                           max_length=30, default=order["pay_method"] or None)
        self.notes = discord.ui.TextInput(label="notes (optional)", style=discord.TextStyle.paragraph,
                                          required=False, max_length=500, default=order["notes"] or None)
        for item in (self.username, self.item, self.quantity, self.method, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        order = await bot.db.get("orders", self.order_id)
        if not order or order["status"] not in ("form", "confirming"):
            return await interaction.response.send_message("⋆｡°✩ this order can't be edited anymore.", ephemeral=True)
        m = re.search(r"\d+", self.quantity.value)
        qty = max(1, min(int(m.group()) if m else 1, 999))
        method = normalize_method(self.method.value)
        if method is None:
            return await interaction.response.send_message(
                "⋆｡°✩ please type **gcash**, **paypal** or **robux** as the mode of payment.", ephemeral=True)
        order = await update_order(bot, order["id"], username=self.username.value.strip(), item=self.item.value.strip(),
                                   quantity=qty, pay_method=method, notes=(self.notes.value or "").strip() or None,
                                   status="confirming")
        await interaction.response.send_message("ʚ♡ɞ got it ! please double check your order below ♡", ephemeral=True)
        channel, guild = interaction.channel, interaction.guild
        old = partial(guild, channel.id, order["confirm_message_id"])
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        msg = await channel.send(content=f"ʚ♡ɞ : is your order correct? <@{order['owner_id']}>",
                                 embeds=[form_embed(guild, order), confirm_embed(guild, order)],
                                 view=ConfirmView(bot))
        await update_order(bot, order["id"], confirm_message_id=msg.id)

    async def on_error(self, interaction, error) -> None:
        log.exception("Order modal error", exc_info=error)


class OrderFormView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="✿ fill order form", style=discord.ButtonStyle.primary, custom_id="uw:o:form")
    async def form(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ no order found here.", ephemeral=True)
        if not await owner_check(interaction, order):
            return
        if order["status"] not in ("form", "confirming"):
            return await interaction.response.send_message("⋆｡°✩ your order is already confirmed.", ephemeral=True)
        await interaction.response.send_modal(OrderModal(self.bot, order))


class ConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="yes, proceed", style=discord.ButtonStyle.success, custom_id="uw:o:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = self.bot
        order = await order_by_channel(bot, interaction.channel_id)
        if not order or not await owner_check(interaction, order):
            return
        if order["status"] != "confirming":
            return await interaction.response.send_message("⋆｡°✩ this order is already confirmed.", ephemeral=True)
        order = await update_order(bot, order["id"], status="payment")
        await interaction.response.edit_message(view=locked_view())
        shop = await get_shop(bot, interaction.guild_id)
        msg = await interaction.channel.send(content=f"<@{order['owner_id']}>",
                                             embed=payment_embed(interaction.guild, shop, order), view=PayView(bot))
        await update_order(bot, order["id"], pay_message_id=msg.id)

    @discord.ui.button(label="edit order", style=discord.ButtonStyle.secondary, custom_id="uw:o:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order or not await owner_check(interaction, order):
            return
        if order["status"] != "confirming":
            return await interaction.response.send_message("⋆｡°✩ this order is already confirmed.", ephemeral=True)
        await interaction.response.send_modal(OrderModal(self.bot, order))


# ---------------------------------------------------------------------------
# flow: payment
# ---------------------------------------------------------------------------
async def send_payment_method(interaction: discord.Interaction, bot, method: str) -> None:
    order = await order_by_channel(bot, interaction.channel_id)
    if not order or not await owner_check(interaction, order):
        return
    if order["status"] not in ("payment", "paid_review"):
        return await interaction.response.send_message("⋆｡°✩ payment isn't open for this order.", ephemeral=True)
    shop = await get_shop(bot, interaction.guild_id)
    await update_order(bot, order["id"], pay_method=method)
    info = shop[f"{method}_info"] or "your handler will send the details shortly ♡"
    desc = f"**{method} payment**\n\n{info}"
    if method == "robux":
        desc += f"\n\n📌 {shop['robux_note']}"
    desc += "\n\nwhen done, press **payment sent** ♡"
    await interaction.response.send_message(embed=discord.Embed(description=rt(interaction.guild, desc), color=PINK))


class PaymentSentModal(discord.ui.Modal, title="payment sent"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.ref = discord.ui.TextInput(label="reference no. / note", required=False, max_length=200,
                                        placeholder="you can also send a screenshot in this channel")
        self.add_item(self.ref)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot, guild = self.bot, interaction.guild
        order = await order_by_channel(bot, interaction.channel_id)
        if not order or order["status"] not in ("payment", "paid_review"):
            return await interaction.response.send_message("⋆｡°✩ nothing to confirm right now.", ephemeral=True)
        ref = (self.ref.value or "").strip() or None
        order = await update_order(bot, order["id"], status="paid_review", payment_ref=ref)
        await interaction.response.send_message("ʚ♡ɞ thank u ! your handler will verify it shortly ♡", ephemeral=True)
        ticket = await bot.db.get("tickets", order["ticket_id"])
        ping = await staff_mentions(bot, guild, ticket)
        desc = (f"✿ **payment sent** · `{order['code']}`\n"
                f"{order['quantity']}x {order['item']} · {order['pay_method'] or '—'}\n"
                f"ref : {ref or 'none (check the screenshot above)'}")
        msg = await interaction.channel.send(content=ping or None, embed=discord.Embed(description=desc, color=PINK),
                                             view=ProofView(bot))
        await update_order(bot, order["id"], proof_message_id=msg.id)

    async def on_error(self, interaction, error) -> None:
        log.exception("Payment modal error", exc_info=error)


class PriceModal(discord.ui.Modal):
    """Staff: set the price (and optionally the deal). With finalize=True it also lines the order up."""

    def __init__(self, bot, order: dict, deals: list[dict], *, finalize: bool):
        super().__init__(title="confirm payment" if finalize else "set price")
        self.bot, self.order_id, self.finalize = bot, order["id"], finalize
        self.price = discord.ui.TextInput(label="amount (php)", max_length=12, placeholder="39",
                                          default=None if order["price"] is None else f"{order['price']:g}")
        current = next((d["name"] for d in deals if d["id"] == order["deal_id"]), None)
        self.deal = discord.ui.TextInput(label="deal name (optional, uses a slot)", required=False, max_length=60,
                                         default=current, placeholder=", ".join(d["name"] for d in deals)[:100] or None)
        self.add_item(self.price)
        self.add_item(self.deal)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot, guild = self.bot, interaction.guild
        order = await bot.db.get("orders", self.order_id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ order not found.", ephemeral=True)
        price = parse_price(self.price.value)
        if price is None:
            return await interaction.response.send_message("⋆｡°✩ please enter a valid amount, e.g. `39`.", ephemeral=True)
        deal_id = None
        name = (self.deal.value or "").strip()
        if name:
            deal = await bot.db.fetchone("SELECT * FROM deals WHERE guild_id = ? AND lower(name) = lower(?)",
                                         (guild.id, name))
            if not deal:
                return await interaction.response.send_message(f"⋆｡°✩ no deal named **{name}**.", ephemeral=True)
            holds_slot = order["slot_held"] and order["deal_id"] == deal["id"]
            if self.finalize and deal["slots_max"] and deal["slots_used"] >= deal["slots_max"] and not holds_slot:
                return await interaction.response.send_message(
                    f"⋆｡°✩ **{deal['name']}** is full ({deal['slots_used']}/{deal['slots_max']}).", ephemeral=True)
            deal_id = deal["id"]
        order = await update_order(bot, order["id"], price=price, deal_id=deal_id)
        if not self.finalize:
            return await interaction.response.send_message(
                f"ʚ♡ɞ price set to **{fmt_price(price)}**", ephemeral=False)
        await interaction.response.defer(ephemeral=True)
        await line_up(bot, guild, interaction.channel, order, interaction.user)
        await interaction.followup.send("ʚ♡ɞ payment confirmed, order lined up.", ephemeral=True)

    async def on_error(self, interaction, error) -> None:
        log.exception("Price modal error", exc_info=error)


class PayView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="♡ gcash", style=discord.ButtonStyle.secondary, custom_id="uw:o:pay:gcash", row=0)
    async def gcash(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_payment_method(interaction, self.bot, "gcash")

    @discord.ui.button(label="♡ paypal", style=discord.ButtonStyle.secondary, custom_id="uw:o:pay:paypal", row=0)
    async def paypal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_payment_method(interaction, self.bot, "paypal")

    @discord.ui.button(label="♡ robux", style=discord.ButtonStyle.secondary, custom_id="uw:o:pay:robux", row=0)
    async def robux(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_payment_method(interaction, self.bot, "robux")

    @discord.ui.button(label="payment sent", style=discord.ButtonStyle.success, custom_id="uw:o:sent", row=1)
    async def sent(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order or not await owner_check(interaction, order):
            return
        if order["status"] not in ("payment", "paid_review"):
            return await interaction.response.send_message("⋆｡°✩ payment isn't open for this order.", ephemeral=True)
        await interaction.response.send_modal(PaymentSentModal(self.bot))

    @discord.ui.button(label="set price (staff)", style=discord.ButtonStyle.secondary, custom_id="uw:o:price", row=1)
    async def price(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ no order found here.", ephemeral=True)
        ticket = await self.bot.db.get("tickets", order["ticket_id"])
        if not await staff_check(interaction, self.bot, ticket):
            return
        deals = await self.bot.db.where("deals", order="position, id", guild_id=interaction.guild_id)
        await interaction.response.send_modal(PriceModal(self.bot, order, deals, finalize=False))


class ProofView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="confirm payment", style=discord.ButtonStyle.success, custom_id="uw:o:ok")
    async def ok(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ no order found here.", ephemeral=True)
        ticket = await self.bot.db.get("tickets", order["ticket_id"])
        if not await staff_check(interaction, self.bot, ticket):
            return
        if order["status"] != "paid_review":
            return await interaction.response.send_message("⋆｡°✩ this payment was already handled.", ephemeral=True)
        deals = await self.bot.db.where("deals", order="position, id", guild_id=interaction.guild_id)
        await interaction.response.send_modal(PriceModal(self.bot, order, deals, finalize=True))

    @discord.ui.button(label="reject", style=discord.ButtonStyle.danger, custom_id="uw:o:no")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ no order found here.", ephemeral=True)
        ticket = await self.bot.db.get("tickets", order["ticket_id"])
        if not await staff_check(interaction, self.bot, ticket):
            return
        if order["status"] != "paid_review":
            return await interaction.response.send_message("⋆｡°✩ this payment was already handled.", ephemeral=True)
        await update_order(self.bot, order["id"], status="payment")
        await interaction.response.edit_message(view=locked_view("rejected"))
        await interaction.channel.send(
            content=f"<@{order['owner_id']}>",
            embed=discord.Embed(description="⋆｡°✩ we couldn't verify your payment yet.\n"
                                            "please resend the proof (screenshot / reference) and press "
                                            "**payment sent** again ♡", color=PINK))


# ---------------------------------------------------------------------------
# flow: queue, status, slots, vouch
# ---------------------------------------------------------------------------
async def adjust_slot(bot, guild: discord.Guild, order: dict, *, hold: bool) -> dict:
    """Take or release the deal slot for an order (idempotent)."""
    if hold and not order["slot_held"] and order["deal_id"]:
        await bot.db.execute("UPDATE deals SET slots_used = slots_used + 1 WHERE id = ?", (order["deal_id"],))
        order = await update_order(bot, order["id"], slot_held=1)
    elif not hold and order["slot_held"] and order["deal_id"]:
        await bot.db.execute("UPDATE deals SET slots_used = MAX(slots_used - 1, 0) WHERE id = ?", (order["deal_id"],))
        order = await update_order(bot, order["id"], slot_held=0)
    await refresh_deals(bot, guild)
    return order


async def line_up(bot, guild: discord.Guild, channel: discord.TextChannel, order: dict, actor) -> None:
    shop = await get_shop(bot, guild.id)
    order = await adjust_slot(bot, guild, order, hold=True)
    order = await update_order(bot, order["id"], status="lined_up")

    # buyer-facing queue message inside the ticket
    ticket_msg = await channel.send(content=f"<@{order['owner_id']}>", embed=await ticket_status_embed(bot, guild, order))
    # staff-facing queue message (buttons) in the queue channel, or inside the ticket when none is set
    qch = guild.get_channel(shop["queue_channel_id"] or 0) or channel
    qmsg = await qch.send(embed=await queue_embed(bot, order), view=QueueView(bot))
    order = await update_order(bot, order["id"], ticket_queue_message_id=ticket_msg.id,
                               queue_channel_id=qch.id, queue_message_id=qmsg.id)
    if proof := partial(guild, channel.id, order["proof_message_id"]):
        try:
            await proof.edit(view=locked_view("confirmed"))
        except discord.HTTPException:
            pass
    await safe_dm(guild, order["owner_id"], embed=discord.Embed(
        description=f"✿ your order `{order['code']}` is lined up ♡ ({fmt_price(order['price'])})", color=PINK))
    ticket = await bot.db.get("tickets", order["ticket_id"])
    from .tickets import log_event
    await log_event(bot, guild, ticket, "order_lined_up", getattr(actor, "id", None), f"{order['code']} {fmt_price(order['price'])}")


async def refresh_order_messages(bot, guild: discord.Guild, order: dict) -> None:
    if m := partial(guild, order["channel_id"], order["ticket_queue_message_id"]):
        try:
            await m.edit(embed=await ticket_status_embed(bot, guild, order))
        except discord.HTTPException:
            pass
    if m := partial(guild, order["queue_channel_id"], order["queue_message_id"]):
        try:
            await m.edit(embed=await queue_embed(bot, order), view=QueueView(bot) if order["status"] != "done" else None)
        except discord.HTTPException:
            pass


async def set_status(bot, guild: discord.Guild, order: dict, status: str, actor) -> dict:
    order = await update_order(bot, order["id"], status=status)
    if status == "done":
        order = await adjust_slot(bot, guild, order, hold=False)
    await refresh_order_messages(bot, guild, order)
    channel = guild.get_channel(order["channel_id"])
    if channel:
        if status == "processing":
            await channel.send(content=f"<@{order['owner_id']}>", embed=discord.Embed(
                description=f"✿ your order `{order['code']}` is now **processing** ♡", color=PINK))
        elif status == "done":
            await channel.send(content=f"<@{order['owner_id']}>", embed=discord.Embed(
                description=f"✿ your order `{order['code']}` is **donesies** !\n"
                            "tenchu for ordering ♡ would you like to leave a vouch?", color=PINK),
                view=VouchView(bot))
    await safe_dm(guild, order["owner_id"], embed=discord.Embed(
        description=f"✿ your order `{order['code']}` is now **{STAGE_LABEL.get(status, status)}** ♡", color=PINK))
    ticket = await bot.db.get("tickets", order["ticket_id"])
    from .tickets import log_event
    await log_event(bot, guild, ticket, f"order_{status}", getattr(actor, "id", None), order["code"])
    return order


class QueueView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _step(self, interaction: discord.Interaction, status: str, allowed: tuple[str, ...]) -> None:
        order = await order_by_queue_message(self.bot, interaction.message.id)
        if not order:
            return await interaction.response.send_message("⋆｡°✩ order not found for this message.", ephemeral=True)
        ticket = await self.bot.db.get("tickets", order["ticket_id"])
        if not await staff_check(interaction, self.bot, ticket):
            return
        if order["status"] not in allowed:
            return await interaction.response.send_message(
                f"⋆｡°✩ order is currently **{STAGE_LABEL.get(order['status'], order['status'])}**.", ephemeral=True)
        await interaction.response.defer()
        await set_status(self.bot, interaction.guild, order, status, interaction.user)

    @discord.ui.button(label="processing", style=discord.ButtonStyle.secondary, custom_id="uw:o:processing")
    async def processing(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._step(interaction, "processing", ("lined_up",))

    @discord.ui.button(label="donesies", style=discord.ButtonStyle.secondary, custom_id="uw:o:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._step(interaction, "done", ("lined_up", "processing"))


class VouchModal(discord.ui.Modal, title="leave a vouch"):
    def __init__(self, bot, order: dict):
        super().__init__()
        self.bot, self.order_id = bot, order["id"]
        self.rating = discord.ui.TextInput(label="rating (1-5)", max_length=1, placeholder="5")
        self.comment = discord.ui.TextInput(label="comment", style=discord.TextStyle.paragraph, required=False,
                                            max_length=500, placeholder="tell us how it went ♡")
        self.add_item(self.rating)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot, guild = self.bot, interaction.guild
        order = await bot.db.get("orders", self.order_id)
        if not order or order["status"] != "done":
            return await interaction.response.send_message("⋆｡°✩ you can vouch once the order is done.", ephemeral=True)
        if await bot.db.fetchone("SELECT id FROM vouches WHERE order_id = ?", (order["id"],)):
            return await interaction.response.send_message("⋆｡°✩ you already vouched for this order ♡", ephemeral=True)
        if not self.rating.value.strip().isdigit() or not 1 <= int(self.rating.value) <= 5:
            return await interaction.response.send_message("⋆｡°✩ rating must be a number from 1 to 5.", ephemeral=True)
        stars = int(self.rating.value)
        comment = (self.comment.value or "").strip()
        await bot.db.insert("vouches", {"guild_id": guild.id, "order_id": order["id"], "user_id": interaction.user.id,
                                        "rating": stars, "comment": comment or None, "created_at": now_iso()})
        shop = await get_shop(bot, guild.id)
        vch = guild.get_channel(shop["vouch_channel_id"] or 0)
        if isinstance(vch, discord.abc.Messageable):
            embed = discord.Embed(description=f"{'★' * stars}{'☆' * (5 - stars)}\n\n{comment or '—'}\n\n"
                                              f"♡ {order['quantity']}x {order['item']}", color=PINK,
                                  timestamp=discord.utils.utcnow())
            embed.set_footer(text=f"vouched by {interaction.user.display_name} · {order['code']}",
                             icon_url=interaction.user.display_avatar.url)
            try:
                await vch.send(embed=embed)
            except discord.HTTPException:
                log.warning("Could not post vouch in %s", vch.id)
        await interaction.response.send_message("ʚ♡ɞ tenchu for the vouch !", ephemeral=True)
        if m := partial(guild, order["channel_id"], interaction.message.id if interaction.message else None):
            try:
                await m.edit(view=locked_view("vouched ♡"))
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error) -> None:
        log.exception("Vouch modal error", exc_info=error)


class VouchView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="♡ leave a vouch", style=discord.ButtonStyle.primary, custom_id="uw:o:vouch")
    async def vouch(self, interaction: discord.Interaction, button: discord.ui.Button):
        order = await order_by_channel(self.bot, interaction.channel_id)
        if not order or not await owner_check(interaction, order):
            return
        if await self.bot.db.fetchone("SELECT id FROM vouches WHERE order_id = ?", (order["id"],)):
            return await interaction.response.send_message("⋆｡°✩ you already vouched for this order ♡", ephemeral=True)
        await interaction.response.send_modal(VouchModal(self.bot, order))


def persistent_views(bot) -> list[discord.ui.View]:
    return [OrderFormView(bot), ConfirmView(bot), PayView(bot), ProofView(bot), QueueView(bot), VouchView(bot)]


async def refresh_deals(bot, guild: discord.Guild) -> None:
    shop = await get_shop(bot, guild.id)
    msg = partial(guild, shop["deals_channel_id"], shop["deals_message_id"])
    if not msg:
        return
    deals = await bot.db.where("deals", order="position, id", guild_id=guild.id)
    try:
        await msg.edit(embed=deals_embed(guild, deals))
    except discord.HTTPException:
        log.warning("Could not refresh the deals message")


# ---------------------------------------------------------------------------
# messages: pledge phrase, auto-responder, activity tracking
# ---------------------------------------------------------------------------
async def handle_message(bot, message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return
    ticket = await bot.db.fetchone("SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (message.channel.id,))
    if not ticket:
        return
    guild = message.guild
    settings = await get_settings(bot, guild.id)
    category = await bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
    staff = is_staff(message.author, settings, category)
    updates = {"last_activity": now_iso(), "idle_warned": 0}
    if staff and message.author.id != ticket["owner_id"]:
        updates["staff_replied"] = 1
    await bot.db.update("tickets", ticket["id"], updates)
    if staff:
        return

    shop = await get_shop(bot, guild.id)
    content = (message.content or "").strip()
    # 1) pledge phrase
    if message.author.id == ticket["owner_id"]:
        order = await order_by_channel(bot, message.channel.id)
        if order and order["status"] == "pledge" and content.lower() == shop["pledge_phrase"].strip().lower():
            return await accept_pledge(bot, message, order)
    # 2) keyword auto-responder
    if not shop["autoreply_enabled"] or not content:
        return
    if ticket["claimed_by"] and not shop["autoreply_when_claimed"]:
        return
    now = time.monotonic()
    for row in await bot.db.where("autoreplies", order="id", guild_id=guild.id, enabled=1):
        if not reply_matches(row, content):
            continue
        key = (message.channel.id, row["id"])
        if now - _reply_seen.get(key, -1e9) < REPLY_COOLDOWN:
            continue
        _reply_seen[key] = now
        text = render(row["response"], user=message.author.mention, shop=shop["shop_name"])
        await message.channel.send(rt(guild, text)[:2000], reference=message, mention_author=False)
        break                                              # one auto-reply per message


# ---------------------------------------------------------------------------
# background watcher: unclaimed nudge, idle reminder, optional auto-close
# ---------------------------------------------------------------------------
async def watch_once(bot) -> None:
    now = discord.utils.utcnow()
    for ticket in await bot.db.fetchall("SELECT * FROM tickets WHERE status = 'open'"):
        guild = bot.get_guild(ticket["guild_id"])
        channel = guild.get_channel(ticket["channel_id"]) if guild else None
        if channel is None:
            continue
        shop = await get_shop(bot, guild.id)
        created = parse_iso(ticket["created_at"]) or now
        last = parse_iso(ticket["last_activity"]) or created
        order = await order_by_channel(bot, channel.id)
        busy = bool(order and order["status"] in ACTIVE_QUEUE)

        # nobody from staff has shown up yet
        mins = shop["unclaimed_minutes"]
        if (mins and not ticket["nudged"] and not ticket["claimed_by"] and not ticket["staff_replied"]
                and now - created >= timedelta(minutes=mins)):
            await bot.db.update("tickets", ticket["id"], {"nudged": 1})
            ping = await staff_mentions(bot, guild, ticket)
            await channel.send(content=ping or None, embed=discord.Embed(
                description=rt(guild, f"ʚ♡ɞ tenchu for waiting <@{ticket['owner_id']}> ! a handler will be with u soon, "
                                      "no need to ping :3"), color=PINK))
            continue

        # buyer went quiet
        hours = shop["idle_hours"]
        if not hours or busy:
            continue
        if not ticket["idle_warned"] and now - last >= timedelta(hours=hours):
            await bot.db.update("tickets", ticket["id"], {"idle_warned": 1})
            extra = (f" this ticket will close automatically in {shop['autoclose_hours']}h if there's no reply."
                     if shop["autoclose_hours"] else "")
            await channel.send(content=f"<@{ticket['owner_id']}>", embed=discord.Embed(
                description=f"✿ still there? we haven't heard from you in a while.{extra}", color=PINK))
        elif (ticket["idle_warned"] and shop["autoclose_hours"]
              and now - last >= timedelta(hours=hours + shop["autoclose_hours"])):
            from .tickets import close_ticket
            await close_ticket(bot, channel, ticket["id"], guild.me)


async def watcher(bot) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await watch_once(bot)
        except Exception:
            log.exception("Ticket watcher failed")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# /shop admin commands
# ---------------------------------------------------------------------------
async def gate(interaction: discord.Interaction, bot) -> bool:
    settings = await get_settings(bot, interaction.guild_id)
    if is_admin(interaction.user, settings):
        return True
    await interaction.response.send_message("⋆｡°✩ administrator only.", ephemeral=True)
    return False


class PaymentSettingsModal(discord.ui.Modal, title="payment details"):
    def __init__(self, bot, shop: dict):
        super().__init__()
        self.bot = bot
        mk = lambda label, key, **kw: discord.ui.TextInput(label=label, default=shop[key], required=False,
                                                          style=discord.TextStyle.paragraph, max_length=500, **kw)
        self.gcash = mk("gcash details", "gcash_info")
        self.paypal = mk("paypal details", "paypal_info")
        self.robux = mk("robux details", "robux_info")
        self.note = discord.ui.TextInput(label="robux note", default=shop["robux_note"], required=False, max_length=200)
        for i in (self.gcash, self.paypal, self.robux, self.note):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await shop_update(self.bot, interaction.guild_id, gcash_info=self.gcash.value or None,
                          paypal_info=self.paypal.value or None, robux_info=self.robux.value or None,
                          robux_note=self.note.value or "")
        await interaction.response.send_message("ʚ♡ɞ payment details saved.", ephemeral=True)


class ReplyModal(discord.ui.Modal):
    def __init__(self, bot, row: dict | None = None):
        super().__init__(title="edit auto-reply" if row else "new auto-reply")
        self.bot, self.row = bot, row
        self.trigger = discord.ui.TextInput(label="keywords (comma separated)", max_length=200,
                                            placeholder="price, how much, magkano", default=row["trigger"] if row else None)
        self.response = discord.ui.TextInput(label="reply", style=discord.TextStyle.paragraph, max_length=1500,
                                             placeholder="use {user} and {shop}; :server_emoji: works",
                                             default=row["response"] if row else None)
        self.match = discord.ui.TextInput(label="match: contains / exact", max_length=8, required=False,
                                          default=row["match_type"] if row else "contains")
        for i in (self.trigger, self.response, self.match):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        match = "exact" if (self.match.value or "").strip().lower() == "exact" else "contains"
        data = {"trigger": self.trigger.value.strip(), "response": self.response.value.strip(), "match_type": match}
        if self.row:
            await self.bot.db.update("autoreplies", self.row["id"], data)
            msg = f"ʚ♡ɞ auto-reply **#{self.row['id']}** updated."
        else:
            rid = await self.bot.db.insert("autoreplies", {"guild_id": interaction.guild_id, **data})
            msg = f"ʚ♡ɞ auto-reply **#{rid}** added."
        await interaction.response.send_message(msg, ephemeral=True)


class DealModal(discord.ui.Modal):
    def __init__(self, bot, row: dict | None = None):
        super().__init__(title="edit deal" if row else "new deal")
        self.bot, self.row = bot, row
        self.name = discord.ui.TextInput(label="name", max_length=60, default=row["name"] if row else None)
        self.price = discord.ui.TextInput(label="price", max_length=30, placeholder="₱39", default=row["price"] if row else None)
        self.inclusions = discord.ui.TextInput(label="inclusions", style=discord.TextStyle.paragraph, max_length=500,
                                               default=row["inclusions"] if row else None, required=False)
        self.slots = discord.ui.TextInput(label="slots (0 = unlimited)", max_length=3,
                                          default=str(row["slots_max"]) if row else "0")
        for i in (self.name, self.price, self.inclusions, self.slots):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.slots.value.strip().isdigit():
            return await interaction.response.send_message("⋆｡°✩ slots must be a number.", ephemeral=True)
        data = {"name": self.name.value.strip(), "price": self.price.value.strip(),
                "inclusions": (self.inclusions.value or "").strip(), "slots_max": int(self.slots.value)}
        clash = await self.bot.db.fetchone("SELECT id FROM deals WHERE guild_id = ? AND lower(name) = lower(?)",
                                           (interaction.guild_id, data["name"]))
        if clash and (not self.row or clash["id"] != self.row["id"]):
            return await interaction.response.send_message("⋆｡°✩ a deal with that name already exists.", ephemeral=True)
        if self.row:
            await self.bot.db.update("deals", self.row["id"], data)
            msg = f"ʚ♡ɞ deal **{data['name']}** updated."
        else:
            await self.bot.db.insert("deals", {"guild_id": interaction.guild_id, **data})
            msg = f"ʚ♡ɞ deal **{data['name']}** added."
        await interaction.response.send_message(msg, ephemeral=True)
        await refresh_deals(self.bot, interaction.guild)


async def category_names(interaction: discord.Interaction, current: str, bot) -> list:
    rows = await bot.db.where("categories", order="name", guild_id=interaction.guild_id)
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows
            if current.lower() in r["name"].lower()][:25]


def build_shop_group(bot) -> app_commands.Group:
    shop = app_commands.Group(name="shop", description="Shop: orders, payment, deals, auto-replies", guild_only=True)

    # ---- setup / settings -------------------------------------------------
    @shop.command(name="setup", description="Set your shop name, channels and which category is the order category.")
    @app_commands.describe(shop_name="Name used in the pledge, e.g. awngel", order_category="Ticket category that runs the order flow",
                           queue_channel="Where staff get processing/donesies buttons",
                           vouch_channel="Where vouches are posted", updates_channel="Mentioned in queue messages",
                           rules_channel="Mentioned in the pledge", pledge_phrase="What buyers must type (default: i swear)")
    async def setup(interaction: discord.Interaction, shop_name: Optional[str] = None,
                    order_category: Optional[str] = None, queue_channel: Optional[discord.TextChannel] = None,
                    vouch_channel: Optional[discord.TextChannel] = None, updates_channel: Optional[discord.TextChannel] = None,
                    rules_channel: Optional[discord.TextChannel] = None, pledge_phrase: Optional[str] = None):
        if not await gate(interaction, bot):
            return
        values: dict = {}
        if shop_name:
            values["shop_name"] = shop_name[:60]
        if pledge_phrase:
            values["pledge_phrase"] = pledge_phrase[:60]
        for key, ch in (("queue_channel_id", queue_channel), ("vouch_channel_id", vouch_channel),
                        ("updates_channel_id", updates_channel), ("rules_channel_id", rules_channel)):
            if ch:
                values[key] = ch.id
        if order_category:
            cat = await bot.db.fetchone("SELECT * FROM categories WHERE guild_id = ? AND lower(name) = lower(?)",
                                        (interaction.guild_id, order_category))
            if not cat:
                return await interaction.response.send_message(
                    f"⋆｡°✩ no ticket category named **{order_category}**. Create it in `/ticket dashboard` first.", ephemeral=True)
            values["order_category_id"] = cat["id"]
        await shop_update(bot, interaction.guild_id, **values)
        await interaction.response.send_message(f"ʚ♡ɞ saved {len(values)} setting(s). See `/shop settings`.", ephemeral=True)

    @setup.autocomplete("order_category")
    async def _ac_cat(interaction: discord.Interaction, current: str):
        return await category_names(interaction, current, bot)

    @shop.command(name="payment", description="Edit gcash / paypal / robux payment details and the robux note.")
    async def payment(interaction: discord.Interaction):
        if await gate(interaction, bot):
            await interaction.response.send_modal(PaymentSettingsModal(bot, await get_shop(bot, interaction.guild_id)))

    @shop.command(name="timers", description="Unclaimed nudge, idle reminder, auto-close and auto-reply behaviour.")
    @app_commands.describe(unclaimed_minutes="Nudge if no staff replied after N minutes (0 = off)",
                           idle_hours="Remind a quiet buyer after N hours (0 = off)",
                           autoclose_hours="Close N hours after the reminder if still quiet (0 = never)",
                           autoreplies="Turn keyword auto-replies on/off",
                           reply_when_claimed="Keep auto-replying after a ticket is claimed")
    async def timers(interaction: discord.Interaction, unclaimed_minutes: Optional[int] = None,
                     idle_hours: Optional[int] = None, autoclose_hours: Optional[int] = None,
                     autoreplies: Optional[bool] = None, reply_when_claimed: Optional[bool] = None):
        if not await gate(interaction, bot):
            return
        values = {}
        for key, val in (("unclaimed_minutes", unclaimed_minutes), ("idle_hours", idle_hours),
                         ("autoclose_hours", autoclose_hours)):
            if val is not None:
                values[key] = max(0, min(val, 24 * 30))
        if autoreplies is not None:
            values["autoreply_enabled"] = int(autoreplies)
        if reply_when_claimed is not None:
            values["autoreply_when_claimed"] = int(reply_when_claimed)
        await shop_update(bot, interaction.guild_id, **values)
        await interaction.response.send_message(f"ʚ♡ɞ saved {len(values)} setting(s).", ephemeral=True)

    @shop.command(name="settings", description="Overview of the shop configuration.")
    async def settings(interaction: discord.Interaction):
        if not await gate(interaction, bot):
            return
        s = await get_shop(bot, interaction.guild_id)
        cat = await bot.db.get("categories", s["order_category_id"]) if s["order_category_id"] else None
        ch = lambda i: f"<#{i}>" if i else "—"
        lines = [f"**shop name** · {s['shop_name']}",
                 f"**order category** · {cat['name'] if cat else 'any category with “order” in its name'}",
                 f"**pledge phrase** · `{s['pledge_phrase']}`",
                 f"**queue** {ch(s['queue_channel_id'])} · **vouches** {ch(s['vouch_channel_id'])} · "
                 f"**updates** {ch(s['updates_channel_id'])} · **rules** {ch(s['rules_channel_id'])}",
                 f"**payment** · gcash {'✓' if s['gcash_info'] else '✗'} · paypal {'✓' if s['paypal_info'] else '✗'} · "
                 f"robux {'✓' if s['robux_info'] else '✗'}",
                 f"**nudge unclaimed** · {s['unclaimed_minutes']} min · **idle reminder** · {s['idle_hours']} h · "
                 f"**auto-close** · {s['autoclose_hours']} h",
                 f"**auto-replies** · {'on' if s['autoreply_enabled'] else 'off'}"
                 f"{' (also after claim)' if s['autoreply_when_claimed'] else ''}"]
        await interaction.response.send_message(embed=discord.Embed(title="✿ shop settings", description="\n".join(lines),
                                                                    color=PINK), ephemeral=True)

    # ---- auto-replies -----------------------------------------------------
    @shop.command(name="reply-add", description="Add a keyword auto-reply for tickets.")
    async def reply_add(interaction: discord.Interaction):
        if await gate(interaction, bot):
            await interaction.response.send_modal(ReplyModal(bot))

    @shop.command(name="reply-edit", description="Edit an auto-reply by its number.")
    async def reply_edit(interaction: discord.Interaction, number: int):
        if not await gate(interaction, bot):
            return
        row = await bot.db.fetchone("SELECT * FROM autoreplies WHERE id = ? AND guild_id = ?", (number, interaction.guild_id))
        if not row:
            return await interaction.response.send_message("⋆｡°✩ no auto-reply with that number.", ephemeral=True)
        await interaction.response.send_modal(ReplyModal(bot, row))

    @shop.command(name="reply-list", description="List all auto-replies.")
    async def reply_list(interaction: discord.Interaction):
        if not await gate(interaction, bot):
            return
        rows = await bot.db.where("autoreplies", order="id", guild_id=interaction.guild_id)
        if not rows:
            return await interaction.response.send_message("no auto-replies yet. add one with `/shop reply-add`.", ephemeral=True)
        text = "\n".join(f"`#{r['id']}` {'✓' if r['enabled'] else '✗'} **{truncate(r['trigger'], 60)}** → "
                         f"{truncate(r['response'], 80)}" for r in rows)
        await interaction.response.send_message(embed=discord.Embed(title="✿ auto-replies", description=truncate(text, 4000),
                                                                    color=PINK), ephemeral=True)

    @shop.command(name="reply-toggle", description="Turn one auto-reply on or off.")
    async def reply_toggle(interaction: discord.Interaction, number: int):
        if not await gate(interaction, bot):
            return
        row = await bot.db.fetchone("SELECT * FROM autoreplies WHERE id = ? AND guild_id = ?", (number, interaction.guild_id))
        if not row:
            return await interaction.response.send_message("⋆｡°✩ no auto-reply with that number.", ephemeral=True)
        await bot.db.update("autoreplies", number, {"enabled": 0 if row["enabled"] else 1})
        await interaction.response.send_message(f"ʚ♡ɞ #{number} is now {'off' if row['enabled'] else 'on'}.", ephemeral=True)

    @shop.command(name="reply-remove", description="Delete an auto-reply.")
    async def reply_remove(interaction: discord.Interaction, number: int):
        if not await gate(interaction, bot):
            return
        row = await bot.db.fetchone("SELECT id FROM autoreplies WHERE id = ? AND guild_id = ?", (number, interaction.guild_id))
        if not row:
            return await interaction.response.send_message("⋆｡°✩ no auto-reply with that number.", ephemeral=True)
        await bot.db.delete("autoreplies", number)
        await interaction.response.send_message(f"ʚ♡ɞ auto-reply #{number} deleted.", ephemeral=True)

    # ---- deals & slots ----------------------------------------------------
    @shop.command(name="deal-add", description="Add a deal (with optional slot limit).")
    async def deal_add(interaction: discord.Interaction):
        if await gate(interaction, bot):
            await interaction.response.send_modal(DealModal(bot))

    @shop.command(name="deal-edit", description="Edit a deal by its number (see /shop deal-list).")
    async def deal_edit(interaction: discord.Interaction, number: int):
        if not await gate(interaction, bot):
            return
        row = await bot.db.fetchone("SELECT * FROM deals WHERE id = ? AND guild_id = ?", (number, interaction.guild_id))
        if not row:
            return await interaction.response.send_message("⋆｡°✩ no deal with that number.", ephemeral=True)
        await interaction.response.send_modal(DealModal(bot, row))

    @shop.command(name="deal-remove", description="Delete a deal.")
    async def deal_remove(interaction: discord.Interaction, number: int):
        if not await gate(interaction, bot):
            return
        row = await bot.db.fetchone("SELECT id FROM deals WHERE id = ? AND guild_id = ?", (number, interaction.guild_id))
        if not row:
            return await interaction.response.send_message("⋆｡°✩ no deal with that number.", ephemeral=True)
        await bot.db.execute("UPDATE orders SET deal_id = NULL, slot_held = 0 WHERE deal_id = ?", (number,))
        await bot.db.delete("deals", number)
        await interaction.response.send_message(f"ʚ♡ɞ deal #{number} deleted.", ephemeral=True)
        await refresh_deals(bot, interaction.guild)

    @shop.command(name="deal-list", description="List deals with their numbers and slots.")
    async def deal_list(interaction: discord.Interaction):
        if not await gate(interaction, bot):
            return
        rows = await bot.db.where("deals", order="position, id", guild_id=interaction.guild_id)
        text = "\n".join(f"`#{r['id']}` **{r['name']}** · {r['price']} · slots {r['slots_used']}/{r['slots_max'] or '∞'}"
                         for r in rows) or "no deals yet. add one with `/shop deal-add`."
        await interaction.response.send_message(text, ephemeral=True)

    @shop.command(name="deal-slots", description="Manually set how many slots of a deal are used.")
    async def deal_slots(interaction: discord.Interaction, number: int, used: int):
        if not await gate(interaction, bot):
            return
        row = await bot.db.fetchone("SELECT id FROM deals WHERE id = ? AND guild_id = ?", (number, interaction.guild_id))
        if not row:
            return await interaction.response.send_message("⋆｡°✩ no deal with that number.", ephemeral=True)
        await bot.db.update("deals", number, {"slots_used": max(0, used)})
        await interaction.response.send_message("ʚ♡ɞ slots updated.", ephemeral=True)
        await refresh_deals(bot, interaction.guild)

    @shop.command(name="deal-post", description="Post (or move) the live deals & slots message.")
    async def deal_post(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not await gate(interaction, bot):
            return
        target = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        s = await get_shop(bot, interaction.guild_id)
        old = partial(interaction.guild, s["deals_channel_id"], s["deals_message_id"])
        deals = await bot.db.where("deals", order="position, id", guild_id=interaction.guild_id)
        try:
            msg = await target.send(embed=deals_embed(interaction.guild, deals))
        except discord.HTTPException:
            return await interaction.followup.send("⋆｡°✩ I can't post in that channel.", ephemeral=True)
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        await shop_update(bot, interaction.guild_id, deals_channel_id=target.id, deals_message_id=msg.id)
        await interaction.followup.send(f"ʚ♡ɞ deals posted in {target.mention}. It updates itself when slots change.", ephemeral=True)

    # ---- orders -----------------------------------------------------------
    @shop.command(name="orders", description="Recent orders and their status.")
    async def orders_cmd(interaction: discord.Interaction):
        if not await gate(interaction, bot):
            return
        rows = await bot.db.fetchall("SELECT * FROM orders WHERE guild_id = ? ORDER BY id DESC LIMIT 15", (interaction.guild_id,))
        text = "\n".join(f"`{r['code']}` <#{r['channel_id']}> · {r['status']} · {r['quantity']}x {r['item'] or '—'} · "
                         f"{fmt_price(r['price'])}" for r in rows) or "no orders yet."
        await interaction.response.send_message(embed=discord.Embed(title="✿ recent orders", description=text, color=PINK),
                                                ephemeral=True)

    return shop
