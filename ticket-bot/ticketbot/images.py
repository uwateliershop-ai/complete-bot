"""Panel pictures:  /ticket image add · bulk · list · remove · clear

A panel can hold any number of pictures or GIFs. They are posted in order above the panel (10 per message).
Add them one by one with `add` (a link, or an uploaded file), or many at once with `bulk` (one link per line).
Links from Tenor / Giphy pages are posted as text so Discord plays the GIF.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

from . import media, panels
from .admin import make_panel_autocomplete, panel_label, resolve_panel
from .ui import ConfirmView
from .util import (BLURPLE, ERR, OK, QUICK_PREFIX, URL_RE, classify_link, is_expiring_discord_link, truncate)

EMBED_BUDGET = 3800
MODAL_LIMIT = 4000


async def target_panel(bot, interaction: discord.Interaction, text: Optional[str]) -> tuple[dict | None, str | None]:
    """The panel a command is about: the one named, else this channel's quick panel, else the only panel."""
    guild = interaction.guild
    if text:
        row = await resolve_panel(bot, guild, text)
        if row is None:
            return None, f"{ERR} I couldn't find that panel. Pick one from the list, or see `/ticket panels`."
        return row, None
    here = await bot.db.fetchone("SELECT * FROM panels WHERE guild_id = ? AND name = ?",
                                 (guild.id, f"{QUICK_PREFIX}{interaction.channel_id}"))
    if here:
        return here, None
    rows = await bot.db.where("panels", order="id", guild_id=guild.id)
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"{ERR} There are no panels yet. Run `/ticket quickpanel` first."
    return None, f"{ERR} Pick a `panel`, or run this in the channel of a quick panel."


async def finish(bot, guild: discord.Guild, panel: dict, lead: str, repost: bool) -> str:
    """Re-post the panel so a change shows (when it is already posted), and build the reply."""
    fresh = await bot.db.get("panels", panel["id"])
    if not fresh["message_id"]:
        return f"{lead}\nThis panel isn't posted yet. `/ticket publish` posts it with its pictures."
    if not repost:
        return f"{lead}\nNot shown yet. Run `/ticket publish` when you're done adding."
    return f"{lead}\n{await panels.publish(bot, guild, panel['id'])}"


def _warn_links(links: list[str]) -> str:
    if any(is_expiring_discord_link(u) for u in links):
        return (f"\n{ERR} Links to files uploaded in Discord stop working after a while. "
                "Use the `file` option to upload the picture instead.")
    return ""


class BulkImageModal(discord.ui.Modal):
    def __init__(self, bot, panel: dict, label: str, repost: bool):
        super().__init__(title=truncate(f"Add pictures · {label}", 45))
        self.bot, self.panel, self.label, self.repost = bot, panel, label, repost
        self.links = discord.ui.TextInput(
            label="One picture or GIF link per line", style=discord.TextStyle.paragraph, required=True,
            max_length=MODAL_LIMIT, placeholder="https://.../one.png\nhttps://.../two.gif\nhttps://tenor.com/view/...")
        self.add_item(self.links)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        good, bad = media.parse_links(self.links.value)
        if bad:
            shown = ", ".join(f"`{truncate(b, 40)}`" for b in bad[:5])
            return await interaction.response.send_message(
                f"{ERR} These aren't links starting with http(s)://: {shown}\nNothing was added. Run the command again.",
                ephemeral=True)
        if not good:
            return await interaction.response.send_message(f"{ERR} Paste at least one link.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await media.insert_images(self.bot.db, self.panel["id"], [{"url": u, "kind": classify_link(u)} for u in good])
        total = len(await media.list_images(self.bot.db, self.panel["id"]))
        lead = (f"{OK} Added **{len(good)}** picture{'s' if len(good) != 1 else ''} to **{self.label}** "
                f"({total} in total, in the order you wrote them).{_warn_links(good)}")
        await interaction.followup.send(await finish(self.bot, interaction.guild, self.panel, lead, self.repost),
                                        ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        import logging
        logging.getLogger(__name__).exception("bulk image modal failed", exc_info=error)
        msg = f"{ERR} Something went wrong while adding the pictures. Check the bot's console."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def add_image_commands(bot, group: app_commands.Group, admin_gate) -> None:
    db = bot.db
    panel_autocomplete = make_panel_autocomplete(bot)
    image = app_commands.Group(name="image", description="Pictures and GIFs shown above a ticket panel", parent=group)

    @image.command(name="add", description="Add ONE picture or GIF to a panel. Run it again for the next one.")
    @app_commands.describe(
        panel="Which panel (default: this channel's quick panel)",
        link="Picture / GIF link (also Tenor or Giphy page links)",
        file="...or upload the picture / GIF itself",
        position="Where it goes in the list (default: at the end)",
        repost="Re-post the panel now so it shows (default: yes)")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def add(interaction: discord.Interaction, panel: Optional[str] = None, link: Optional[str] = None,
                  file: Optional[discord.Attachment] = None, position: Optional[int] = None, repost: bool = True):
        if not await admin_gate(interaction, bot):
            return
        if bool(link) == bool(file):
            return await interaction.response.send_message(
                f"{ERR} Give either a `link` or a `file`, not both and not neither.", ephemeral=True)
        if position is not None and position < 1:
            return await interaction.response.send_message(f"{ERR} `position` starts at 1.", ephemeral=True)
        row, error = await target_panel(bot, interaction, panel)
        if row is None:
            return await interaction.response.send_message(error, ephemeral=True)
        guild = interaction.guild
        if link:
            link = link.strip()
            if not URL_RE.match(link):
                return await interaction.response.send_message(
                    f"{ERR} The `link` must start with http(s)://", ephemeral=True)
            item = {"url": link, "kind": classify_link(link)}
        else:
            limit = getattr(guild, "filesize_limit", None) or media.DEFAULT_UPLOAD_LIMIT
            if not media.is_image_file(file):
                return await interaction.response.send_message(
                    f"{ERR} That file isn't a picture. Use png, jpg, gif or webp.", ephemeral=True)
            if file.size > limit:
                return await interaction.response.send_message(
                    f"{ERR} That file is {file.size / 1_048_576:.1f} MB; this server's upload limit is "
                    f"{limit / 1_048_576:.0f} MB.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not link:
            item = {"file_path": await media.save_upload(file, guild.id), "kind": "file", "label": file.filename}
        number = await media.insert_images(db, row["id"], [item], position)
        total = len(await media.list_images(db, row["id"]))
        lead = (f"{OK} Picture **#{number}** added to **{panel_label(guild, row)}** ({total} in total)."
                f"{_warn_links([link]) if link else ''}")
        await interaction.followup.send(await finish(bot, guild, row, lead, repost), ephemeral=True)

    @image.command(name="bulk", description="Add many pictures or GIFs at once: one link per line.")
    @app_commands.describe(panel="Which panel (default: this channel's quick panel)",
                           repost="Re-post the panel now so they show (default: yes)")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def bulk(interaction: discord.Interaction, panel: Optional[str] = None, repost: bool = True):
        if not await admin_gate(interaction, bot):
            return
        row, error = await target_panel(bot, interaction, panel)
        if row is None:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.send_modal(BulkImageModal(bot, row, panel_label(interaction.guild, row), repost))

    @image.command(name="list", description="Show a panel's pictures in order, with their numbers.")
    @app_commands.describe(panel="Which panel (default: this channel's quick panel)")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def list_images(interaction: discord.Interaction, panel: Optional[str] = None):
        if not await admin_gate(interaction, bot):
            return
        row, error = await target_panel(bot, interaction, panel)
        if row is None:
            return await interaction.response.send_message(error, ephemeral=True)
        images = await media.list_images(db, row["id"])
        lines = []
        for number, img in enumerate(images, start=1):
            line = truncate(f"`{number}.` {media.describe_image(img)}", 200)
            if sum(len(x) + 1 for x in lines) + len(line) > EMBED_BUDGET:
                break
            lines.append(line)
        embed = discord.Embed(title=f"୨୧ Pictures · {truncate(panel_label(interaction.guild, row), 150)}", color=BLURPLE,
                              description="\n".join(lines) or "No pictures yet. Use `/ticket image add`.")
        if lines:
            embed.set_footer(text=f"Showing {len(lines)} of {len(images)} • remove with /ticket image remove <number>")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @image.command(name="remove", description="Remove one picture (by its number), or a range.")
    @app_commands.describe(number="Number from /ticket image list", last="Also remove everything up to this number",
                           panel="Which panel (default: this channel's quick panel)",
                           repost="Re-post the panel now (default: yes)")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def remove(interaction: discord.Interaction, number: int, last: Optional[int] = None,
                     panel: Optional[str] = None, repost: bool = True):
        if not await admin_gate(interaction, bot):
            return
        row, error = await target_panel(bot, interaction, panel)
        if row is None:
            return await interaction.response.send_message(error, ephemeral=True)
        total = len(await media.list_images(db, row["id"]))
        end = number if last is None else last
        if number < 1 or end < number or end > total:
            return await interaction.response.send_message(
                f"{ERR} Pick numbers between 1 and {total}." if total else f"{ERR} This panel has no pictures.",
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        gone = await media.remove_images(db, row["id"], number, end)
        await media.purge_files(db, [g["file_path"] for g in gone])
        lead = f"{OK} Removed **{len(gone)}** picture{'s' if len(gone) != 1 else ''} ({total - len(gone)} left)."
        await interaction.followup.send(await finish(bot, interaction.guild, row, lead, repost), ephemeral=True)

    @image.command(name="clear", description="Remove every picture from a panel (asks first).")
    @app_commands.describe(panel="Which panel (default: this channel's quick panel)")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def clear(interaction: discord.Interaction, panel: Optional[str] = None):
        if not await admin_gate(interaction, bot):
            return
        row, error = await target_panel(bot, interaction, panel)
        if row is None:
            return await interaction.response.send_message(error, ephemeral=True)
        total = len(await media.list_images(db, row["id"]))
        if not total:
            return await interaction.response.send_message(f"{ERR} This panel has no pictures.", ephemeral=True)

        async def yes(i: discord.Interaction):
            await i.response.edit_message(content="Removing pictures…", view=None)
            gone = await media.remove_images(db, row["id"], 1, total)
            await media.purge_files(db, [g["file_path"] for g in gone])
            lead = f"{OK} Removed all {len(gone)} pictures."
            await i.edit_original_response(content=await finish(bot, i.guild, row, lead, True))

        await interaction.response.send_message(
            f"Remove all **{total}** pictures from **{panel_label(interaction.guild, row)}**?", ephemeral=True,
            view=ConfirmView(interaction.user.id, yes, yes_label="Remove all"))
