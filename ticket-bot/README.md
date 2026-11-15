# Discord Ticketing Bot

A fully Discord-configurable ticket bot (Python · discord.py · SQLite). Admins manage **everything** – panels,
embeds, buttons, dropdowns, categories, forms, permissions, transcripts – from a `/ticket dashboard` made of
buttons, select menus and modals. No source-code editing is needed for normal use.

## 1 · Setup

1. **Create the bot** at <https://discord.com/developers/applications> → *Bot* tab:
   - Enable **Server Members Intent** and **Message Content Intent** (needed for staff checks and transcripts).
   - Copy the token.
2. **Invite it** (OAuth2 → URL Generator): scopes `bot` + `applications.commands`; permissions
   *View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Channels, Manage Roles, Manage Messages*.
   Place the bot's role **above** the staff roles it will grant access to.
3. **Run it** (Python 3.10+):
   ```bash
   pip install -r requirements.txt
   cp .env.example .env        # paste DISCORD_TOKEN (optionally DEV_GUILD_ID for instant command sync)
   python main.py
   ```
   Global slash commands can take up to an hour to appear the first time; `DEV_GUILD_ID` makes them instant on one server.

## 2 · First-time walkthrough (all inside Discord)

Run `/ticket dashboard` (needs *Administrator*, or a role you later set under **Permissions**).

1. **Permissions** → pick your *Staff roles* (and optional *Administrator roles*).
2. **Transcript Settings** → choose an archive channel (and optional log channel).
3. **Form Management** → create a form → **Questions** (max 5 – a Discord modal limit).
4. **Category Management** → create a category → set its staff roles, Discord channel category, form, welcome message.
5. **Embed Builder** → create a template (title, description, colour, author, footer, images, **Fields**).
6. **Panel Management** → create a panel → pick its destination channel + embed template(s) →
   **Buttons** / **Dropdowns** (→ **Options**) → each linked to a category → **Publish**.

Published panels **update in place** whenever you change their embeds, buttons, dropdowns or options.
Use **Publish / Republish** to send a fresh copy (the old message is deleted).

## 3 · Commands

| Command | Who | Function |
|---|---|---|
| `/ticket help` | admin | Short guide to every command |
| `/ticket quickpanel` | admin | Build and post a whole panel from one form (re-run in the same channel to edit) |
| `/ticket panels` | admin | List all panels and where they are posted |
| `/ticket publish <panel> [channel]` | admin | Post or re-post a panel; the old message is replaced only after the new one is up. `channel` moves dashboard panels (quick panels stay in their channel) |
| `/ticket unpublish <panel>` | admin | Take a panel down, keep all its settings |
| `/ticket dashboard` | admin | Full configuration: embeds, buttons, dropdowns, categories, forms, permissions, transcripts, general |
| `/ticket settings` | admin | Read-only overview of roles, channels, limits and totals |
| `/ticket list [status] [user]` | admin | Tickets newest first (`open` default · `closed` · `deleted` · `all`), optionally by owner |
| `/ticket info [number]` | staff | Details of a ticket |
| `/ticket close [number]` | owner* / staff | Close a ticket (asks first when *Close confirmation* is on) |
| `/ticket reopen [number]` | staff | Reopen a closed ticket |
| `/ticket delete [number]` | staff | Delete a ticket channel for good (always asks first, saves a transcript first) |
| `/ticket transcript [number]` | staff | Inside a ticket: generate one now. With a number: retrieve the latest saved transcript |

`number` can be left out inside a ticket channel. `panel` autocompletes (quick panels show as *Quick panel · #channel*).
\* Owners can close their own ticket when the category allows it. Staff = staff roles of the category / server, plus admins.
Inside tickets, staff also have buttons: **Claim · Unclaim · Transfer · Close · Transcript · Add user · Remove user · Rename**;
closed tickets show **Reopen · Transcript · Delete**.

New or renamed slash commands can take up to an hour to appear globally; `DEV_GUILD_ID` makes them instant on one server.

## 4 · Behaviour notes

- **Button actions** – `create`: opens the category's form if it has one, else creates the ticket · `form`: opens the button's own form, then creates in its category · `category`: creates straight in the category, skipping forms. Every button/option needs a category.
- **Limits** – *Max open tickets* and *Duplicate prevention* (one open ticket per category per user) are enforced; double-clicks can't create duplicates.
- **Naming** – placeholders `{number}` `{username}` `{category}`; the 4-digit ticket number is always preserved, also after renames. Welcome messages accept `{user}` `{category}` `{number}`.
- **Closing** – owner (if the category allows) and staff can close; owner access becomes read-only; transcript is generated and archived automatically when enabled. **Deleting** always saves a transcript first; DB records and transcript files survive.
- **Transcripts** – self-contained HTML (messages, markdown, embeds, form answers, activity log). Images ≤ 1.5 MB are embedded so they survive Discord's expiring CDN links. Files are stored in `data/transcripts/<guild>/` and posted to the archive channel.
- **Logs** – all 10 activity types go to the `ticket_logs` table and, if configured, a log channel.
- **Dropdown min/max** – stored and sent to Discord; when several options are selected, only the first opens a ticket.
- **Webhooks** – the embed builder uses the bot's own interaction system (components on webhook messages require extra handling). Posting via real webhooks (custom name/avatar) is not included; it's an optional later add-on.
- **Discord limits honoured** – 25 select options · 5 buttons per row · 5 rows per message (each dropdown uses a full row) · 5 modal inputs · 25 embed fields · 10 embeds per message. Panels that exceed the row limit show a clear error instead of failing.

## 5 · Project layout

```
main.py                 entry point, persistent-view restore, command sync
ticketbot/db.py         SQLite schema + async helpers (aiosqlite)
ticketbot/entities.py   declarative spec of every configurable item (drives the generic CRUD UI)
ticketbot/crud.py       dashboard, list/edit screens, modals, pickers
ticketbot/panels.py     embed builder, panel components, publish + live sync
ticketbot/tickets.py    ticket creation + all management actions
ticketbot/transcripts.py HTML transcripts, archive, retrieval
ticketbot/commands.py   /ticket slash command tree (help, dashboard, transcript) + registers the modules below
ticketbot/quick.py      /ticket quickpanel (one-form panel setup)
ticketbot/admin.py      /ticket panels · publish · unpublish · settings · list · info · close · reopen · delete
tests/                  offline regression test (fake discord/aiosqlite, no network)
```

Back up `data/tickets.db` and `data/transcripts/` to keep your configuration and archives.

## 6 · Roles & security

All configuration screens and commands require Administrator, the server owner, or a configured admin role – checked on every click, not just on opening. Ticket owners can only close their own ticket (if the category allows it). Only staff can claim, transfer, add/remove users, rename, reopen, delete or generate transcripts.

## 7 · Shop layer (orders, payment, queue, deals, auto-replies)

Everything below is configured from Discord with the `/shop` commands (see `/ticket help`).

**Order flow** (runs in any ticket whose category is the *order category*; set it with `/shop setup order_category:`,
or just name a category "Order"):
pledge (buyer types the phrase, default `i swear`) → order form → order confirmation (`yes, proceed` / `edit order`) →
payment method buttons (gcash · paypal · robux) → `payment sent` → staff `confirm payment` (price + optional deal) →
**lined up** → `processing` → `donesies` → `leave a vouch`.
Staff-only buttons are checked on every click. The queue message (with `processing` / `donesies`) goes to the
queue channel, or stays in the ticket if none is set. The buyer sees a progress bar in the ticket and gets DMs.

**Deals & slots** – `/shop deal-add`, then `/shop deal-post` posts a live message. Confirming a payment with a deal
takes a slot (blocked when full); `donesies` frees it; the message edits itself.

**Auto-responder** – `/shop reply-add` keyword replies (`{user}`, `{shop}`, `:server_emoji:`), 5-minute cooldown per
ticket, paused once a ticket is claimed (`/shop timers reply_when_claimed:true` to keep going). Also: one nudge to staff
when nobody answered after N minutes, an idle reminder to quiet buyers, optional auto-close. Orders being worked on are never nagged.

Tests: `python tests/orders_test.py` (offline).
