"""/ticket post : the bot posts a plain message (no embed). Text, :server_emoji:, {sticker:Name}, uploaded files."""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands

from . import media
from .util import ERR, OK, truncate

TEXT_LIMIT = 2000


class PostModal(discord.ui.Modal):
    def __init__(self, bot, channel, files: list):
        super().__init__(title=truncate(f"Message for #{channel.name}", 45))
        self.bot, self.channel, self.files = bot, channel, files
        self.text = discord.ui.TextInput(
            label="Message (:emoji: {sticker:Name} ok)", style=discord.TextStyle.paragraph,
            required=not files, max_length=TEXT_LIMIT, placeholder="Write it exactly as it should appear.")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rich = media.rich_text(interaction.guild, (self.text.value or "").strip())
        if not (rich.text or rich.stickers or self.files):
            return await interaction.response.send_message(f"{ERR} The message is empty.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        kwargs: dict = {"allowed_mentions": discord.AllowedMentions(everyone=False, roles=True, users=True)}
        if rich.text:
            kwargs["content"] = rich.text
        if rich.stickers:
            kwargs["stickers"] = rich.stickers
        try:
            if self.files:
                kwargs["files"] = [await f.to_file() for f in self.files]
            try:
                sent = await self.channel.send(**kwargs)
            except discord.HTTPException:
                if not rich.stickers:
                    raise
                kwargs.pop("stickers")                      # e.g. stickers not allowed in that channel
                if self.files:
                    kwargs["files"] = [await f.to_file() for f in self.files]
                sent = await self.channel.send(**kwargs)
        except discord.HTTPException as exc:
            return await interaction.followup.send(
                f"{ERR} I couldn't post in {self.channel.mention}: {truncate(getattr(exc, 'text', exc), 200)}",
                ephemeral=True)
        notes = [f"{ERR} There is no sticker called **{n}** in this server." for n in rich.missing]
        await interaction.followup.send("\n".join([f"{OK} Posted in {self.channel.mention}.", *notes]), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logging.getLogger(__name__).exception("post failed", exc_info=error)
        msg = f"{ERR} Something went wrong. Check the bot's console."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def add_post_command(bot, group: app_commands.Group, admin_gate) -> None:
    @group.command(name="post", description="Make the bot post a normal message (no embed), with optional files.")
    @app_commands.describe(channel="Where to post it (default: this channel)",
                           file="Picture / GIF / file to attach (optional)",
                           file2="Another file (optional)", file3="Another file (optional)")
    async def post(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                  file: Optional[discord.Attachment] = None, file2: Optional[discord.Attachment] = None,
                  file3: Optional[discord.Attachment] = None):
        if not await admin_gate(interaction, bot):
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.abc.Messageable):
            return await interaction.response.send_message(f"{ERR} Pick a text channel.", ephemeral=True)
        await interaction.response.send_modal(PostModal(bot, target, [f for f in (file, file2, file3) if f]))


# ---------------------------------------------------------------------------
# /ticket imageurl : picture in, link out
# ---------------------------------------------------------------------------
UPLOAD_API = "https://catbox.moe/user/api.php"       # free anonymous host; links are permanent and public
MAX_BYTES = 20 * 1024 * 1024


async def upload_to_host(data: bytes, filename: str) -> str:
    """Upload a picture to the host and return its link. Raises RuntimeError with a readable reason."""
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("fileToUpload", data, filename=filename)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(UPLOAD_API, data=form) as resp:
                body = (await resp.text()).strip()
                if resp.status != 200 or not body.startswith("http"):
                    raise RuntimeError(truncate(body or f"HTTP {resp.status}", 150))
                return body
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"could not reach the image host ({exc.__class__.__name__})")


def add_imageurl_command(bot, group: app_commands.Group, admin_gate) -> None:
    @group.command(name="imageurl", description="Turn an uploaded picture or GIF into a link you can paste anywhere.")
    @app_commands.describe(file="The picture or GIF to convert")
    async def imageurl(interaction: discord.Interaction, file: discord.Attachment):
        if not await admin_gate(interaction, bot):
            return
        if not media.is_image_file(file):
            return await interaction.response.send_message(
                f"{ERR} That file isn't a picture. Use png, jpg, gif or webp.", ephemeral=True)
        if file.size > MAX_BYTES:
            return await interaction.response.send_message(
                f"{ERR} That file is too big (max {MAX_BYTES // 1_048_576} MB).", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            url = await upload_to_host(await file.read(), file.filename)
        except RuntimeError as exc:
            return await interaction.followup.send(f"{ERR} Upload failed: {exc}.", ephemeral=True)
        await interaction.followup.send(
            f"{OK} Your link (permanent, and anyone with it can open the picture):\n{url}\n"
            "Paste it into any link box: thumbnail, bottom bar icon, embed images, `/ticket image add`.",
            ephemeral=True)
