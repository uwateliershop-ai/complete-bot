"""Vouches: when a ticket closes the owner gets a vouch button (in the ticket and by DM).
The vouch form has a rating, the item/service, the staff who helped, a comment and an optional image;
it is posted to the vouch channel set in /dashboard -> Vouch Settings. One vouch per ticket."""
from __future__ import annotations

import io
import logging
import sqlite3

import discord

from . import media, texts
from .util import get_settings, truncate

log = logging.getLogger(__name__)

PINK = 0xFFB7D5
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def now_iso() -> str:
    return discord.utils.utcnow().isoformat(timespec="seconds")


async def already_vouched(bot, ticket_id: int) -> bool:
    return bool(await bot.db.fetchone("SELECT id FROM ticket_vouches WHERE ticket_id = ?", (ticket_id,)))


def vouch_view(ticket_id: int, *, disabled: bool = False, label: str | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VouchButton(ticket_id, disabled=disabled, label=label))
    return view


# ---------------------------------------------------------------------------
# the button (persistent: the ticket id is inside the custom_id, so it also works in DMs after restarts)
# ---------------------------------------------------------------------------
class VouchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tb:vouch:(?P<id>[0-9]+)"):
    def __init__(self, ticket_id: int, *, disabled: bool = False, label: str | None = None):
        super().__init__(discord.ui.Button(
            label=label or "♡ Leave a vouch", style=discord.ButtonStyle.success,
            custom_id=f"tb:vouch:{ticket_id}", disabled=disabled))
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ticket = await bot.db.get("tickets", self.ticket_id)
        if not ticket:
            return await interaction.response.send_message("⋆｡°✩ This ticket no longer exists.", ephemeral=True)
        if interaction.user.id != ticket["owner_id"]:
            return await interaction.response.send_message("⋆｡°✩ Only the ticket owner can vouch.", ephemeral=True)
        if await already_vouched(bot, ticket["id"]):
            return await interaction.response.send_message("⋆｡°✩ You already vouched for this ticket ♡", ephemeral=True)
        category = await bot.db.get("categories", ticket["category_id"]) if ticket["category_id"] else None
        await interaction.response.send_modal(VouchModal(bot, ticket, category))


# ---------------------------------------------------------------------------
# the form
# ---------------------------------------------------------------------------
class VouchModal(discord.ui.Modal, title="Leave a vouch ♡"):
    def __init__(self, bot, ticket: dict, category: dict | None):
        super().__init__()
        self.bot, self.ticket_id = bot, ticket["id"]

        self.rating = discord.ui.Select(
            placeholder="Choose a rating",
            options=[discord.SelectOption(label=f"{'★' * n}{'☆' * (5 - n)}  ({n}/5)", value=str(n))
                     for n in (5, 4, 3, 2, 1)])
        self.item = discord.ui.TextInput(
            default=truncate(category["name"], 100) if category else None, max_length=100,
            placeholder="What are you vouching for?")
        staff_default = []
        if ticket["claimed_by"]:
            staff_default = [discord.SelectDefaultValue(id=ticket["claimed_by"],
                                                        type=discord.SelectDefaultValueType.user)]
        self.staff = discord.ui.UserSelect(placeholder="Pick the staff member", required=False,
                                           min_values=0, max_values=1, default_values=staff_default)
        self.comment = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=500,
                                            placeholder="Say something nice (optional)")
        self.image = discord.ui.FileUpload(required=False, min_values=0, max_values=1)

        self.add_item(discord.ui.Label(text="Rating", component=self.rating))
        self.add_item(discord.ui.Label(text="Item / service", component=self.item))
        self.add_item(discord.ui.Label(text="Staff who helped you", description="Leave as is if already filled in.",
                                       component=self.staff))
        self.add_item(discord.ui.Label(text="Comment", component=self.comment))
        self.add_item(discord.ui.Label(text="Attach an image (optional)",
                                       description="A screenshot or photo of your order.", component=self.image))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket = await bot.db.get("tickets", self.ticket_id)
        if not ticket or ticket["owner_id"] != interaction.user.id:
            return await interaction.followup.send("⋆｡°✩ Only the ticket owner can vouch.", ephemeral=True)
        if await already_vouched(bot, ticket["id"]):
            return await interaction.followup.send("⋆｡°✩ You already vouched for this ticket ♡", ephemeral=True)

        guild = bot.get_guild(ticket["guild_id"]) or interaction.guild
        if not self.rating.values:
            return await interaction.followup.send("⋆｡°✩ Please choose a rating from 1 to 5.", ephemeral=True)
        stars = int(self.rating.values[0])
        item = media.emojify(guild, self.item.value.strip()) or "—"
        comment = media.emojify(guild, self.comment.value.strip()) if self.comment.value.strip() else None
        staff_id = self.staff.values[0].id if self.staff.values else ticket["claimed_by"]

        picture = None
        if self.image.values:
            att = self.image.values[0]
            if not media.is_image_file(att):
                return await interaction.followup.send("⋆｡°✩ That file isn't a picture. Please try again with an image.", ephemeral=True)
            if att.size > MAX_IMAGE_BYTES:
                return await interaction.followup.send("⋆｡°✩ That image is too big (8 MB max). Please try a smaller one.", ephemeral=True)
            try:
                picture = discord.File(io.BytesIO(await att.read()), filename=att.filename)
            except discord.HTTPException:
                return await interaction.followup.send("⋆｡°✩ I couldn't read that image. Please try again.", ephemeral=True)

        try:
            await bot.db.insert("ticket_vouches", {
                "guild_id": ticket["guild_id"], "ticket_id": ticket["id"], "user_id": interaction.user.id,
                "rating": stars, "item": item, "staff_id": staff_id, "comment": comment, "created_at": now_iso()})
        except sqlite3.IntegrityError:
            return await interaction.followup.send("⋆｡°✩ You already vouched for this ticket ♡", ephemeral=True)

        settings = await get_settings(bot, ticket["guild_id"])
        embed = discord.Embed(title=f"{'★' * stars}{'☆' * (5 - stars)}  New vouch!", color=PINK,
                              timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Item / Service", value=truncate(item, 1024), inline=False)
        embed.add_field(name="Helped by", value=f"<@{staff_id}>" if staff_id else "*not specified*", inline=True)
        embed.add_field(name="Vouched by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Comment", value=truncate(comment or "*No comment*", 1024), inline=False)
        embed.set_footer(text=f"Ticket #{ticket['number']:04d}")
        if picture:
            embed.set_image(url=f"attachment://{picture.filename}")

        channel = guild.get_channel(settings["vouch_channel_id"] or 0) if guild else None
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(),
                                   **({"file": picture} if picture else {}))
            except discord.HTTPException:
                log.warning("Could not post vouch in channel %s", channel.id)
        else:
            log.warning("Vouch saved but no vouch channel is set (guild %s)", ticket["guild_id"])

        await interaction.followup.send("ʚ♡ɞ Thank you so much for your vouch !", ephemeral=True)
        if interaction.message is not None:           # lock the button the owner just used
            try:
                await interaction.message.edit(view=vouch_view(ticket["id"], disabled=True, label="♡ Vouched"))
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Vouch form error", exc_info=error)
        msg = "⋆｡°✩ Something went wrong with your vouch. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# asking for the vouch (called when a ticket is closed)
# ---------------------------------------------------------------------------
async def send_prompt(bot, channel: discord.TextChannel, ticket: dict) -> None:
    settings = await get_settings(bot, ticket["guild_id"])
    if not settings["vouch_enabled"] or await already_vouched(bot, ticket["id"]):
        return
    guild = channel.guild
    text = texts.say(guild, settings, "vouch_message", user=f"<@{ticket['owner_id']}>",
                     number=f"{ticket['number']:04d}", server=guild.name)
    embed = discord.Embed(title="♡ How did we do?", description=truncate(text, 4000), color=PINK)

    # separate message in the ticket, then a DM (still works after the channel is deleted)
    try:
        await channel.send(content=f"<@{ticket['owner_id']}>", embed=embed, view=vouch_view(ticket["id"]),
                           allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        log.warning("Could not send the vouch request in channel %s", channel.id)
    owner = guild.get_member(ticket["owner_id"])
    if owner:
        dm = discord.Embed(title="♡ How did we do?", color=PINK, description=(
            f"Your ticket **#{ticket['number']:04d}** in **{guild.name}** was closed.\n"
            "Tap below to leave a vouch ♡"))
        try:
            await owner.send(embed=dm, view=vouch_view(ticket["id"]))
        except (discord.Forbidden, discord.HTTPException):
            pass                                       # DMs closed
