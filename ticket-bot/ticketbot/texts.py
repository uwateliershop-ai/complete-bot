"""Every message the bot writes on its own, with its default. Admins change them in /dashboard
(Messages · Order Confirmation · Queue · Transcript · Vouch). Blank = default.

Placeholders are written {like_this}; unknown ones are left alone. `:server_emoji:` becomes the real emoji."""
from __future__ import annotations

from . import media
from .util import render

DEFAULTS = {
    # ticket messages
    "msg_created": "ʚ♡ɞ Your ticket has been created: {channel}",
    "form_title": "୨୧ Ticket form",
    "msg_claim": "♡ {staff} has claimed this ticket.",
    "msg_unclaim": "⟲ {staff} released this ticket.",
    "msg_reopen": "Reopened by {staff}.",
    "msg_close_title": "𐙚 Ticket closed",
    "msg_close_text": "Closed by {staff}.{note}\nStaff can reopen or delete this ticket below.",
    "msg_delete": "⋆ This ticket will be deleted in 5 seconds…",
    # order confirmation (before / after)
    "order_before_title": "୨୧ Confirm your order",
    "order_before_text": "Please check your order, {user}.\nPress **{yes}** to continue or **{edit}** to change something.",
    "order_yes_label": "Yes, proceed",
    "order_edit_label": "Edit order",
    "order_after_title": "ʚ♡ɞ Order confirmed",
    "order_after_text": "Thank you {user}! Order **{code}** is confirmed and you are number **{position}** in the queue.\n"
                        "We'll update you here when it moves.",
    # queue
    "queue_title": "♡ Order {code}",
    "queue_text": "{user} · {category}\nStatus: **{status}**",
    "queue_btn_processing": "Processing",
    "queue_btn_done": "Done",
    "stage_queued": "In the queue",
    "stage_processing": "Processing",
    "stage_done": "Done",
    "queue_update_text": "✿ Your order **{code}** is now **{status}**.",
    "board_title": "୨୧ Order queue",
    "board_text": "Orders waiting or being worked on. Updated automatically.",
    # transcript
    "tr_title": "୨୧ Ticket #{number} transcript",
    "tr_footer": "Generated {time}",
    "tr_archive_title": "୨୧ Transcript – Ticket #{number}",
    "tr_archive_text": "",
    "vouch_message": "Thank you for choosing us {user}! Would you like to leave a vouch? ♡",
}


def raw(settings: dict | None, key: str) -> str:
    value = (settings or {}).get(key)
    return value if value else DEFAULTS.get(key, "")


def say(guild, settings: dict | None, key: str, **values) -> str:
    """The text for `key` with placeholders filled in and :server_emoji: turned into emoji."""
    return media.rich_text(guild, render(raw(settings, key), **values)).text
