"""Declarative description of every configurable item.

The dashboard / slash commands are generated from these specs, which is what
gives every item full Create / Read / Update / Delete support from inside Discord.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import URL_RE, valid_emoji


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"        # text | para | int | color | url | emoji | choice
    required: bool = False
    placeholder: str = ""
    max: int = 100            # max text length in the modal
    choices: tuple = ()
    default: Any = None
    lo: int = 0               # int bounds
    hi: int = 1000


@dataclass
class Picker:
    key: str
    label: str
    kind: str                 # roles | channel | category | ref | refs
    ref: str | None = None    # entity name for ref / refs


@dataclass
class Entity:
    name: str
    table: str
    title: str
    singular: str
    icon: str
    groups: dict[str, list[Field]]
    pickers: list[Picker] = field(default_factory=list)
    toggles: dict[str, str] = field(default_factory=dict)
    name_col: str = "name"
    parent: str | None = None
    parent_col: str | None = None
    scoped: bool = True            # table has a guild_id column
    ordered: bool = False          # has a position column
    enable_col: str | None = None
    max_children: int | None = None
    singleton: bool = False        # one row per guild (guild_settings)


STYLE_CHOICES = ("primary", "secondary", "success", "danger")
ACTION_CHOICES = ("create", "form", "category")

PANEL = Entity(
    "panel", "panels", "Ticket Panels", "panel", "⋆",
    groups={
        "Basics": [
            Field("name", "Panel name", required=True),
            Field("title", "Embed title (if no template)", max=256),
            Field("description", "Description (if no template)", "para", max=2000),
            Field("color", "Color hex, e.g. #5865F2", "color", max=7),
        ],
        "Images": [
            Field("image_url", "Banner image URL", "url", max=500),
            Field("thumbnail_url", "Thumbnail URL", "url", max=500),
        ],
    },
    pickers=[
        Picker("channel_id", "Destination channel", "channel"),
        Picker("embed_ids", "Embed templates", "refs", "embed"),
    ],
    enable_col="enabled",
)

EMBED = Entity(
    "embed", "embed_templates", "Embed Templates", "embed template", "⋆",
    groups={
        "Main": [
            Field("name", "Template name", required=True),
            Field("title", "Title", max=256),
            Field("description", "Description (markdown, emojis, links)", "para", max=4000),
            Field("color", "Color hex, e.g. #5865F2", "color", max=7),
        ],
        "Author": [
            Field("author_name", "Author name", max=256),
            Field("author_icon", "Author icon URL", "url", max=500),
        ],
        "Footer & Images": [
            Field("footer_text", "Footer text", max=2048),
            Field("footer_icon", "Footer icon URL", "url", max=500),
            Field("thumbnail_url", "Thumbnail URL", "url", max=500),
            Field("image_url", "Banner / image URL", "url", max=500),
        ],
    },
    toggles={"show_timestamp": "Timestamp"},
)

EMBED_FIELD = Entity(
    "embed_field", "embed_fields", "Embed Fields", "embed field", "⋆",
    groups={"Field": [
        Field("name", "Field name", required=True, max=256),
        Field("value", "Field value", "para", required=True, max=1024),
    ]},
    toggles={"is_inline": "Inline"},
    parent="embed", parent_col="embed_id", scoped=False, ordered=True, max_children=25,
)

BUTTON = Entity(
    "button", "panel_buttons", "Panel Buttons", "button", "⋆",
    groups={"Button": [
        Field("label", "Label", required=True, max=80),
        Field("emoji", "Emoji (♡ · :name: · server emoji)", "emoji", max=64,
              placeholder="♡   or   :server_emoji:   or   <:name:123456789012345678>"),
        Field("style", "Style: primary/secondary/success/danger", "choice",
              required=True, choices=STYLE_CHOICES, default="primary", max=10),
        Field("action", "Action: create / form / category", "choice",
              required=True, choices=ACTION_CHOICES, default="create", max=10),
    ]},
    pickers=[
        Picker("category_id", "Ticket category", "ref", "category"),
        Picker("form_id", "Form (for 'form' action)", "ref", "form"),
    ],
    name_col="label", parent="panel", parent_col="panel_id", scoped=False,
    ordered=True, enable_col="enabled", max_children=25,
)

DROPDOWN = Entity(
    "dropdown", "panel_dropdowns", "Dropdown Menus", "dropdown menu", "⋆",
    groups={"Dropdown": [
        Field("placeholder", "Placeholder text", required=True, max=150),
        Field("min_values", "Minimum selections (0-25)", "int", default=1, lo=0, hi=25, max=2),
        Field("max_values", "Maximum selections (1-25)", "int", default=1, lo=1, hi=25, max=2),
    ]},
    name_col="placeholder", parent="panel", parent_col="panel_id", scoped=False,
    ordered=True, enable_col="enabled", max_children=5,
)

OPTION = Entity(
    "option", "dropdown_options", "Dropdown Options", "dropdown option", "⋆",
    groups={"Option": [
        Field("label", "Label", required=True, max=100),
        Field("description", "Description", max=100),
        Field("emoji", "Emoji (♡ · :name: · server emoji)", "emoji", max=64,
              placeholder="♡   or   :server_emoji:   or   <:name:123456789012345678>"),
    ]},
    pickers=[Picker("category_id", "Ticket category", "ref", "category")],
    name_col="label", parent="dropdown", parent_col="dropdown_id", scoped=False,
    ordered=True, enable_col="enabled", max_children=25,
)

CATEGORY = Entity(
    "category", "categories", "Ticket Categories", "category", "⋆",
    groups={"Main": [
        Field("name", "Category name", required=True),
        Field("description", "Description", "para", max=300),
        Field("naming_format", "Ticket name format, e.g. {category}-{number}", max=80),
        Field("welcome_message", "Welcome message ({user} {category} {number})", "para", max=2000),
    ]},
    pickers=[
        Picker("staff_role_ids", "Staff roles", "roles"),
        Picker("discord_category_id", "Discord channel category", "category"),
        Picker("form_id", "Ticket form", "ref", "form"),
    ],
    toggles={"owner_can_close": "Owner can close", "mention_staff": "Ping staff"},
    enable_col="enabled",
)

FORM = Entity(
    "form", "forms", "Ticket Forms", "form", "⋆",
    groups={"Form": [Field("name", "Form name", required=True)]},
)

QUESTION = Entity(
    "question", "form_questions", "Form Questions", "question", "⋆",
    groups={"Question": [
        Field("label", "Question (max 45 chars)", required=True, max=45),
        Field("placeholder", "Placeholder text", max=100),
        Field("style", "Style: short / paragraph", "choice", required=True,
              choices=("short", "paragraph"), default="short", max=10),
    ]},
    toggles={"required": "Required"},
    name_col="label", parent="form", parent_col="form_id", scoped=False,
    ordered=True, max_children=5,   # Discord modals hold at most 5 inputs
)

# The three settings screens all edit the single guild_settings row.
GENERAL = Entity(
    "general", "guild_settings", "General Settings", "general settings", "⋆",
    groups={"General": [
        Field("max_open_tickets", "Max open tickets per user (0 = unlimited)", "int",
              required=True, default=3, lo=0, hi=100, max=3),
        Field("naming_format", "Ticket name format, e.g. ticket-{number}", required=True, max=80,
              default="ticket-{number}"),
        Field("welcome_message", "Welcome message ({user} {category} {number})", "para",
              required=True, max=2000),
    ]},
    pickers=[Picker("ticket_category_id", "Default ticket channel category", "category")],
    toggles={"prevent_duplicates": "Duplicate prevention", "close_confirmation": "Close confirmation"},
    name_col="id", scoped=False, singleton=True,
)

PERMS = Entity(
    "permissions", "guild_settings", "Permissions", "permissions", "⋆",
    groups={},
    pickers=[
        Picker("staff_role_ids", "Staff roles", "roles"),
        Picker("admin_role_ids", "Administrator roles", "roles"),
    ],
    name_col="id", scoped=False, singleton=True,
)

TRANSCRIPTS = Entity(
    "transcripts", "guild_settings", "Transcript Settings", "transcript settings", "୨୧",
    groups={},
    pickers=[
        Picker("archive_channel_id", "Transcript archive channel", "channel"),
        Picker("log_channel_id", "Ticket log channel", "channel"),
    ],
    toggles={"auto_transcript": "Auto transcripts"},
    name_col="id", scoped=False, singleton=True,
)

ENTITIES: dict[str, Entity] = {
    e.name: e for e in (
        PANEL, EMBED, EMBED_FIELD, BUTTON, DROPDOWN, OPTION, CATEGORY, FORM, QUESTION,
        GENERAL, PERMS, TRANSCRIPTS,
    )
}


def parse_field(f: Field, raw: str):
    """Validate a modal answer and convert it to the DB value."""
    raw = (raw or "").strip()
    if f.kind == "int":
        if not raw:
            return f.default
        try:
            value = int(raw)
        except ValueError:
            raise ValueError("must be a whole number")
        if not f.lo <= value <= f.hi:
            raise ValueError(f"must be between {f.lo} and {f.hi}")
        return value
    if f.kind == "choice":
        value = raw.lower() or f.default
        if value not in f.choices:
            raise ValueError("must be one of: " + ", ".join(f.choices))
        return value
    if not raw:
        if f.required:
            raise ValueError("this field is required")
        return None
    if f.kind == "color":
        text = raw.lstrip("#")
        if len(text) != 6:
            raise ValueError("use a 6-digit hex color like #5865F2")
        try:
            return int(text, 16)
        except ValueError:
            raise ValueError("use a 6-digit hex color like #5865F2")
    if f.kind == "url":
        if not URL_RE.match(raw):
            raise ValueError("must be a valid http(s) URL")
        return raw
    if f.kind == "emoji":
        if not valid_emoji(raw):
            raise ValueError("use a short symbol like ♡, or one of this server's emojis written as :name: "
                             "(check the spelling), or <:name:123456789012345678>")
        return raw
    return raw


def display_value(f: Field, value) -> str:
    """Value shown inside a modal text box / detail embed."""
    if value is None:
        return ""
    if f.kind == "color":
        return f"#{int(value):06X}"
    return str(value)
