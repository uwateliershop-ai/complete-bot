"""SQLite persistence layer (aiosqlite)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite

from .util import classify_link, jload

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    id INTEGER PRIMARY KEY,                       -- = guild id
    max_open_tickets INTEGER NOT NULL DEFAULT 3,
    prevent_duplicates INTEGER NOT NULL DEFAULT 1,
    naming_format TEXT NOT NULL DEFAULT 'ticket-{number}',
    staff_role_ids TEXT NOT NULL DEFAULT '[]',
    admin_role_ids TEXT NOT NULL DEFAULT '[]',
    ticket_category_id INTEGER,
    welcome_message TEXT NOT NULL DEFAULT 'Welcome {user}! A staff member will be with you shortly.',
    close_confirmation INTEGER NOT NULL DEFAULT 1,
    auto_transcript INTEGER NOT NULL DEFAULT 1,
    archive_channel_id INTEGER,
    log_channel_id INTEGER,
    unclaimed_minutes INTEGER NOT NULL DEFAULT 0,   -- nudge staff if nobody answered after N minutes (0 = off)
    idle_hours INTEGER NOT NULL DEFAULT 0,          -- remind a quiet ticket owner after N hours (0 = off)
    autoclose_hours INTEGER NOT NULL DEFAULT 0,     -- close N hours after the reminder (0 = never)
    autoreply_enabled INTEGER NOT NULL DEFAULT 1,   -- master switch for auto-responders
    nudge_message TEXT,
    idle_message TEXT,
    vouch_channel_id INTEGER,
    vouch_enabled INTEGER NOT NULL DEFAULT 1,       -- ask the ticket owner for a vouch when a ticket closes
    vouch_message TEXT,
    ticket_counter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ticket_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    ticket_id INTEGER NOT NULL UNIQUE REFERENCES tickets(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending | queued | processing | done
    confirm_message_id INTEGER,
    form_message_id INTEGER,
    queue_channel_id INTEGER,
    queue_message_id INTEGER,
    handled_by INTEGER,
    created_at TEXT NOT NULL, confirmed_at TEXT, done_at TEXT
);

CREATE TABLE IF NOT EXISTS ticket_vouches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    ticket_id INTEGER NOT NULL UNIQUE REFERENCES tickets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    item TEXT,
    staff_id INTEGER,
    comment TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embed_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    title TEXT, description TEXT,
    color INTEGER DEFAULT 5793266,
    author_name TEXT, author_icon TEXT,
    thumbnail_url TEXT, image_url TEXT,
    footer_text TEXT, footer_icon TEXT,
    show_timestamp INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS embed_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embed_id INTEGER NOT NULL REFERENCES embed_templates(id) ON DELETE CASCADE,
    name TEXT NOT NULL, value TEXT NOT NULL,
    is_inline INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS form_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    label TEXT NOT NULL, placeholder TEXT,
    style TEXT NOT NULL DEFAULT 'short',
    required INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL, description TEXT,
    naming_format TEXT, welcome_message TEXT,
    discord_category_id INTEGER,
    staff_role_ids TEXT NOT NULL DEFAULT '[]',
    form_id INTEGER REFERENCES forms(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    owner_can_close INTEGER NOT NULL DEFAULT 1,
    mention_staff INTEGER NOT NULL DEFAULT 1,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL, title TEXT, description TEXT,
    color INTEGER DEFAULT 5793266,
    image_url TEXT, thumbnail_url TEXT,
    channel_id INTEGER,
    message_id INTEGER, message_channel_id INTEGER,
    embed_ids TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    content TEXT,                                 -- plain text above the embeds (outside any card)
    decor_message_id INTEGER,                     -- legacy banner message (older versions), removed on re-publish
    image_message_ids TEXT NOT NULL DEFAULT '[]', -- gallery messages posted above the panel
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS panel_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    url TEXT,                                     -- picture / GIF link
    file_path TEXT,                               -- our own copy of an uploaded picture
    label TEXT,                                   -- original file name of an upload (for /image list)
    kind TEXT NOT NULL DEFAULT 'image',           -- image | link (Tenor/Giphy page) | file
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_panel_images_panel ON panel_images(panel_id, position);

CREATE TABLE IF NOT EXISTS panel_buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    label TEXT NOT NULL, emoji TEXT,
    style TEXT NOT NULL DEFAULT 'primary',
    action TEXT NOT NULL DEFAULT 'create',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    form_id INTEGER REFERENCES forms(id) ON DELETE SET NULL,
    url TEXT,                                     -- action 'link'
    reply_text TEXT,                              -- action 'reply'
    reply_embed_id INTEGER REFERENCES embed_templates(id) ON DELETE SET NULL,
    role_id INTEGER,                              -- action 'role' (toggle)
    ephemeral INTEGER NOT NULL DEFAULT 1,         -- reply only visible to the clicker
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS panel_dropdowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    placeholder TEXT NOT NULL,
    min_values INTEGER NOT NULL DEFAULT 1,
    max_values INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dropdown_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dropdown_id INTEGER NOT NULL REFERENCES panel_dropdowns(id) ON DELETE CASCADE,
    label TEXT NOT NULL, description TEXT, emoji TEXT,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    panel_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',          -- open | closed | deleted
    claimed_by INTEGER,
    form_answers TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    closed_at TEXT, closed_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets(guild_id, owner_id, status);

CREATE TABLE IF NOT EXISTS ticket_participants (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    added_by INTEGER, added_at TEXT,
    PRIMARY KEY (ticket_id, user_id)
);

CREATE TABLE IF NOT EXISTS ticket_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    ticket_id INTEGER NOT NULL,
    ticket_number INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    ticket_number INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    archive_channel_id INTEGER,
    archive_message_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS autoresponders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    trigger TEXT NOT NULL,                        -- comma separated keywords
    match_type TEXT NOT NULL DEFAULT 'contains',  -- contains | exact | starts
    response TEXT,                                -- plain text ({user} {username} {server} {channel} {ticket})
    embed_id INTEGER REFERENCES embed_templates(id) ON DELETE SET NULL,
    scope TEXT NOT NULL DEFAULT 'tickets',        -- tickets | server
    channel_id INTEGER,                           -- scope 'server': only in this channel (NULL = everywhere)
    cooldown INTEGER NOT NULL DEFAULT 60,         -- seconds, per channel
    reply INTEGER NOT NULL DEFAULT 1,             -- reply to the member's message instead of a plain send
    staff_triggers INTEGER NOT NULL DEFAULT 0,    -- staff messages can trigger it too
    when_claimed INTEGER NOT NULL DEFAULT 0,      -- keep answering after a ticket was claimed
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_autoresponders_guild ON autoresponders(guild_id);

CREATE TABLE IF NOT EXISTS autoresponder_buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    responder_id INTEGER NOT NULL REFERENCES autoresponders(id) ON DELETE CASCADE,
    label TEXT NOT NULL, emoji TEXT,
    style TEXT NOT NULL DEFAULT 'secondary',
    action TEXT NOT NULL DEFAULT 'reply',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    form_id INTEGER REFERENCES forms(id) ON DELETE SET NULL,
    url TEXT,
    reply_text TEXT,
    reply_embed_id INTEGER REFERENCES embed_templates(id) ON DELETE SET NULL,
    role_id INTEGER,
    ephemeral INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);
"""


NEW_SETTING_COLUMNS = tuple((name, "TEXT") for name in (
    "msg_created", "form_title", "msg_claim", "msg_unclaim", "msg_reopen", "msg_close_title", "msg_close_text",
    "msg_delete", "tr_title", "tr_footer", "tr_theme", "tr_archive_title", "tr_archive_text",
    "order_before_title", "order_before_text", "order_yes_label", "order_edit_label", "order_after_title",
    "order_after_text", "queue_title", "queue_text", "queue_btn_processing", "queue_btn_done", "stage_queued",
    "stage_processing", "stage_done", "queue_update_text", "board_title", "board_text")) + (
    ("tr_color", "INTEGER"), ("tr_show_form", "INTEGER NOT NULL DEFAULT 1"),
    ("tr_show_log", "INTEGER NOT NULL DEFAULT 1"), ("tr_show_meta", "INTEGER NOT NULL DEFAULT 1"),
    ("tr_dm_owner", "INTEGER NOT NULL DEFAULT 0"), ("queue_channel_id", "INTEGER"),
    ("queue_dm", "INTEGER NOT NULL DEFAULT 1"), ("board_channel_id", "INTEGER"), ("board_message_id", "INTEGER"))


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        """Add columns that older databases don't have yet."""
        cols = {r["name"] for r in await self.fetchall("PRAGMA table_info(panels)")}
        for name, ddl in (("content", "TEXT"), ("decor_message_id", "INTEGER"),
                          ("image_message_ids", "TEXT NOT NULL DEFAULT '[]'")):
            if name not in cols:
                await self.conn.execute(f"ALTER TABLE panels ADD COLUMN {name} {ddl}")
        tcols = {r["name"] for r in await self.fetchall("PRAGMA table_info(tickets)")}
        for name, ddl in (("last_activity", "TEXT"), ("nudged", "INTEGER NOT NULL DEFAULT 0"),
                          ("idle_warned", "INTEGER NOT NULL DEFAULT 0"),
                          ("staff_replied", "INTEGER NOT NULL DEFAULT 0")):
            if name not in tcols:
                await self.conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}")
        gcols = {r["name"] for r in await self.fetchall("PRAGMA table_info(guild_settings)")}
        for name, ddl in (("unclaimed_minutes", "INTEGER NOT NULL DEFAULT 0"), ("idle_hours", "INTEGER NOT NULL DEFAULT 0"),
                          ("autoclose_hours", "INTEGER NOT NULL DEFAULT 0"), ("autoreply_enabled", "INTEGER NOT NULL DEFAULT 1"),
                          ("nudge_message", "TEXT"), ("idle_message", "TEXT"),
                          ("vouch_channel_id", "INTEGER"), ("vouch_enabled", "INTEGER NOT NULL DEFAULT 1"),
                          ("vouch_message", "TEXT")) + NEW_SETTING_COLUMNS:
            if name not in gcols:
                await self.conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {name} {ddl}")
        pcols2 = {r["name"] for r in await self.fetchall("PRAGMA table_info(panels)")}
        if "layout" not in pcols2:
            await self.conn.execute("ALTER TABLE panels ADD COLUMN layout TEXT NOT NULL DEFAULT 'classic'")
        ccols = {r["name"] for r in await self.fetchall("PRAGMA table_info(categories)")}
        if "is_order" not in ccols:
            await self.conn.execute("ALTER TABLE categories ADD COLUMN is_order INTEGER NOT NULL DEFAULT 0")
        bcols = {r["name"] for r in await self.fetchall("PRAGMA table_info(panel_buttons)")}
        for name, ddl in (("url", "TEXT"), ("reply_text", "TEXT"),
                          ("reply_embed_id", "INTEGER REFERENCES embed_templates(id) ON DELETE SET NULL"),
                          ("role_id", "INTEGER"), ("ephemeral", "INTEGER NOT NULL DEFAULT 1")):
            if name not in bcols:
                await self.conn.execute(f"ALTER TABLE panel_buttons ADD COLUMN {name} {ddl}")
        await self._migrate_quick_banners()
        await self._migrate_legacy_shop()

    async def _migrate_legacy_shop(self) -> None:
        """One-time: carry timers and keyword auto-replies over from the old shop layer (tables are left untouched)."""
        if (await self.fetchone("PRAGMA user_version"))["user_version"] >= 1:
            return
        tables = {r["name"] for r in await self.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
        claimed: dict[int, int] = {}
        if "shop_settings" in tables:
            for s in await self.fetchall("SELECT * FROM shop_settings"):
                await self.conn.execute("INSERT OR IGNORE INTO guild_settings (id) VALUES (?)", (s["guild_id"],))
                await self.conn.execute(
                    "UPDATE guild_settings SET unclaimed_minutes = ?, idle_hours = ?, autoclose_hours = ?, "
                    "autoreply_enabled = ? WHERE id = ?",
                    (s["unclaimed_minutes"], s["idle_hours"], s["autoclose_hours"], s["autoreply_enabled"], s["guild_id"]))
                claimed[s["guild_id"]] = s["autoreply_when_claimed"]
        if "autoreplies" in tables:
            for r in await self.fetchall("SELECT * FROM autoreplies ORDER BY id"):
                first = (r["trigger"].split(",")[0].strip() or "reply")[:40]
                await self.conn.execute(
                    "INSERT INTO autoresponders (guild_id, name, trigger, match_type, response, scope, cooldown, "
                    "when_claimed, enabled) VALUES (?, ?, ?, ?, ?, 'tickets', 300, ?, ?)",
                    (r["guild_id"], f"{first} (#{r['id']})", r["trigger"], r["match_type"], r["response"],
                     claimed.get(r["guild_id"], 0), r["enabled"]))
        await self.conn.execute("PRAGMA user_version = 1")

    async def _migrate_quick_banners(self) -> None:
        """Older quick panels kept their banner in a hidden embed template; it is now the first gallery image."""
        for panel in await self.fetchall("SELECT * FROM panels WHERE name LIKE 'quick-%'"):
            tpl = await self.fetchone("SELECT * FROM embed_templates WHERE guild_id = ? AND name = ?",
                                      (panel["guild_id"], panel["name"] + "-banner"))
            if not tpl:
                continue
            has_images = await self.fetchone("SELECT id FROM panel_images WHERE panel_id = ? LIMIT 1", (panel["id"],))
            if tpl["image_url"] and not has_images:
                await self.conn.execute(
                    "INSERT INTO panel_images (panel_id, url, kind, position) VALUES (?, ?, ?, 0)",
                    (panel["id"], tpl["image_url"], classify_link(tpl["image_url"])))
            ids = [i for i in jload(panel["embed_ids"]) if i != tpl["id"]]
            await self.conn.execute(
                "UPDATE panels SET embed_ids = ?, thumbnail_url = COALESCE(thumbnail_url, ?) WHERE id = ?",
                (json.dumps(ids), tpl["thumbnail_url"], panel["id"]))
            await self.conn.execute("DELETE FROM embed_templates WHERE id = ?", (tpl["id"],))

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # -- raw helpers ---------------------------------------------------
    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self.conn.execute(sql, params) as cur:
            last = cur.lastrowid
        await self.conn.commit()
        return last

    # -- generic CRUD (table / column names always come from code, never users)
    async def get(self, table: str, row_id: int) -> dict | None:
        return await self.fetchone(f"SELECT * FROM {table} WHERE id = ?", (row_id,))

    async def where(self, table: str, order: str = "rowid", **conds) -> list[dict]:
        clause = " AND ".join(f"{k} = ?" for k in conds) or "1"
        return await self.fetchall(
            f"SELECT * FROM {table} WHERE {clause} ORDER BY {order}", tuple(conds.values())
        )

    async def insert(self, table: str, data: dict) -> int:
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        return await self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))

    async def update(self, table: str, row_id: int, data: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in data)
        await self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))

    async def delete(self, table: str, row_id: int) -> None:
        await self.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))

    async def next_ticket_number(self, guild_id: int) -> int:
        async with self.lock:
            await self.execute("INSERT OR IGNORE INTO guild_settings (id) VALUES (?)", (guild_id,))
            await self.execute(
                "UPDATE guild_settings SET ticket_counter = ticket_counter + 1 WHERE id = ?", (guild_id,)
            )
            row = await self.fetchone("SELECT ticket_counter FROM guild_settings WHERE id = ?", (guild_id,))
            return row["ticket_counter"]
