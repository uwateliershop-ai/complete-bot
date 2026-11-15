"""Offline test for auto-responders, card buttons, timers and the legacy migration: python tests/autoresponder_test.py
Uses the same fake discord/aiosqlite stand-ins as offline_smoke_test.py, so no token or network is needed."""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stubs
import discord
from types import SimpleNamespace as NS
from datetime import timedelta

os.environ["TRANSCRIPT_DIR"] = tempfile.mkdtemp()
from ticketbot.db import Database
from ticketbot import actions, autoresponder, automation, crud, panels
from ticketbot.entities import ENTITIES


class FakeResponse:
    def __init__(s): s.done = False; s.log = []
    def is_done(s): return s.done
    async def send_message(s, *a, **k): s.done = True; s.log.append(("send", a, k))
    async def edit_message(s, **k): s.done = True; s.log.append(("edit", (), k))
    async def send_modal(s, m): s.done = True; s.log.append(("modal", (m,), {}))
    async def defer(s, **k): s.done = True; s.log.append(("defer", (), k))

class FakeMember(discord.Member):
    def __init__(s, id, guild, admin=False, roles=()):
        s.id, s.guild, s.bot, s.roles = id, guild, False, list(roles)
        s.name = s.display_name = f"user{id}"; s.mention = f"<@{id}>"
        s.guild_permissions = NS(administrator=admin); s.display_avatar = NS(url="https://x/a.png")
        s.dms = []
    async def send(s, **k): s.dms.append(k)

class FakeMsg:
    _n = 5000
    def __init__(s, **k): FakeMsg._n += 1; s.id = FakeMsg._n; s.__dict__.update(k); s.pinned = False; s.edits = []
    async def edit(s, **k): s.edits.append(k)
    async def delete(s): s.deleted = True
    async def pin(s): s.pinned = True

class FakeChannel(discord.TextChannel, discord.abc.Messageable):
    def __init__(s, id, name, guild):
        s.id, s.name, s.guild, s.sent, s.msgs = id, name, guild, [], {}
        s.mention = f"<#{id}>"
    async def send(s, *a, **k):
        m = FakeMsg(**k); s.sent.append(k); s.msgs[m.id] = m; return m
    def get_partial_message(s, mid): return s.msgs.get(mid) or FakeMsg(id=mid)

class FakeGuild(discord.Guild):
    def __init__(s):
        s.id = 1; s.name = "G"; s.owner_id = 999; s.roles = {10: discord.Role(10, "Staff")}
        s.channels, s.members = {}, {}
        s.me = FakeMember(500, s, admin=True)
    def get_channel(s, i): return s.channels.get(i)
    def get_role(s, i): return s.roles.get(i)
    def get_member(s, i): return s.members.get(i)

class FakeInteraction:
    def __init__(s, user, guild, channel, message=None):
        s.user, s.guild, s.guild_id, s.channel, s.channel_id, s.message = user, guild, guild.id, channel, channel.id, message
        s.response, s.followups = FakeResponse(), []
        s.followup = NS(send=s._fu)
    async def _fu(s, *a, **k): s.followups.append((a, k))

def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: ok.failed = True
ok.failed = False

def press(view, label_part):
    return next(c for c in view.children if label_part in (c.label or ""))



async def main():
    E = ENTITIES
    # ---- matching
    r = {"trigger": "price, how much", "match_type": "contains"}
    ok(autoresponder.matches(r, "How much is it?") and not autoresponder.matches(r, "priceless art"), "contains uses whole words")
    ok(autoresponder.matches({"trigger": "hi", "match_type": "exact"}, "Hi") and not autoresponder.matches({"trigger": "hi", "match_type": "exact"}, "hi there"), "exact match")
    ok(autoresponder.matches({"trigger": "!rules", "match_type": "starts"}, "!rules please") and not autoresponder.matches({"trigger": "!rules", "match_type": "starts"}, "see !rules"), "starts match")

    # ---- environment
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db")); await db.connect()
    guild = FakeGuild()
    staff = FakeMember(2, guild, admin=True); member = FakeMember(3, guild)
    guild.members.update({2: staff, 3: member})
    tch = FakeChannel(100, "ticket-0001", guild); gen = FakeChannel(101, "general", guild); other = FakeChannel(102, "other", guild)
    for c in (tch, gen, other): guild.channels[c.id] = c
    bot = NS(db=db, get_guild=lambda i: guild, get_channel=lambda i: guild.get_channel(i), guilds=[guild])
    bot.add_view = lambda *a, **k: None
    cat = await db.insert("categories", {"guild_id": 1, "name": "Support", "staff_role_ids": "[]"})
    tid = await db.insert("tickets", {"guild_id": 1, "number": 1, "channel_id": 100, "owner_id": 3, "category_id": cat,
                                      "status": "open", "created_at": autoresponder.now_iso()})
    msg = lambda text, who=member, ch=tch: NS(guild=guild, author=who, channel=ch, content=text)

    # ---- an auto-responder with text, an embed card and three kinds of button
    emb = await db.insert("embed_templates", {"guild_id": 1, "name": "Prices", "title": "Price list", "description": "see below",
                                              "color": 5793266, "show_timestamp": 0})
    rid = await db.insert("autoresponders", {"guild_id": 1, "name": "price", "trigger": "price, magkano", "match_type": "contains",
                                             "response": "hi {user} / {username} / {server} / {shop} / #{ticket}", "embed_id": emb})
    await db.insert("autoresponder_buttons", {"responder_id": rid, "label": "Website", "action": "link", "url": "https://example.com", "position": 0})
    await db.insert("autoresponder_buttons", {"responder_id": rid, "label": "More info", "action": "reply", "reply_text": "details for {user}",
                                              "reply_embed_id": emb, "position": 1})
    await db.insert("autoresponder_buttons", {"responder_id": rid, "label": "Broken", "action": "link", "position": 2})   # no url
    await db.insert("autoresponder_buttons", {"responder_id": rid, "label": "Off", "action": "reply", "reply_text": "x", "enabled": 0, "position": 3})

    await autoresponder.handle_message(bot, msg("magkano po?"))
    sent = tch.sent[-1] if tch.sent else {}
    ok(len(tch.sent) == 1, "auto-reply fires on a keyword inside a ticket")
    ok("hi <@3> / user3 / G / G / #0001" in sent.get("content", ""), "placeholders {user} {username} {server} {shop} {ticket}")
    ok(len(sent.get("embeds", [])) == 1 and sent["embeds"][0].title == "Price list", "the embed card is attached")
    kids = sent["view"].children
    ok([getattr(c, "item", c).label for c in kids] == ["Website", "More info"], "only working, enabled buttons are shown")
    ok(kids[0].url == "https://example.com" and kids[0].custom_id is None, "link button has a url and no callback id")
    ok(isinstance(kids[1], autoresponder.ResponderButton) and kids[1].custom_id.startswith("tb:ar:"), "other buttons are persistent dynamic items")
    ok(sent["reference"] is not None and sent["allowed_mentions"] is not None, "replies to the message, mentions restricted")
    ok((await db.get("tickets", tid))["last_activity"], "activity recorded on every ticket message")

    await autoresponder.handle_message(bot, msg("price again?"))
    ok(len(tch.sent) == 1, "same responder is on cooldown")
    await autoresponder.handle_message(bot, msg("unrelated"))
    ok(len(tch.sent) == 1, "no keyword, no reply")

    # ---- the reply button runs its action (fresh config, private by default)
    more_id = (await db.where("autoresponder_buttons", order="id", responder_id=rid))[1]["id"]
    i = FakeInteraction(member, guild, tch)
    i.client = bot
    await autoresponder.ResponderButton({"id": more_id, "label": "More info", "emoji": None, "style": "secondary"}).callback(i)
    log = i.response.log[0][2]
    ok(log["content"] == "details for <@3>" and log["ephemeral"] is True and len(log["embeds"]) == 1, "reply button: private text + embed")
    await db.update("autoresponder_buttons", more_id, {"ephemeral": 0})
    i = FakeInteraction(member, guild, tch); i.client = bot
    await autoresponder.ResponderButton({"id": more_id, "label": "x", "emoji": None, "style": "secondary"}).callback(i)
    ok(i.response.log[0][2]["ephemeral"] is False, "reply button can be made public")
    await db.update("autoresponder_buttons", more_id, {"enabled": 0})
    i = FakeInteraction(member, guild, tch); i.client = bot
    await autoresponder.ResponderButton({"id": more_id, "label": "x", "emoji": None, "style": "secondary"}).callback(i)
    ok("no longer available" in i.response.log[0][1][0], "disabled button answers politely")

    # ---- switches: master, per-responder, claimed, staff, cooldown 0
    autoresponder._seen.clear()
    await db.update("guild_settings", 1, {"autoreply_enabled": 0})
    await autoresponder.handle_message(bot, msg("price"))
    ok(len(tch.sent) == 1, "master switch turns every auto-responder off")
    await db.update("guild_settings", 1, {"autoreply_enabled": 1})
    await db.update("autoresponders", rid, {"enabled": 0})
    await autoresponder.handle_message(bot, msg("price"))
    ok(len(tch.sent) == 1, "a disabled responder stays quiet")
    await db.update("autoresponders", rid, {"enabled": 1})
    await db.update("tickets", tid, {"claimed_by": 2})
    await autoresponder.handle_message(bot, msg("price"))
    ok(len(tch.sent) == 1, "silent once a ticket is claimed")
    await db.update("autoresponders", rid, {"when_claimed": 1})
    await autoresponder.handle_message(bot, msg("price"))
    ok(len(tch.sent) == 2, "...unless 'keep replying after claimed' is on")
    await autoresponder.handle_message(bot, msg("price", who=staff))
    ok(len(tch.sent) == 2, "staff don't trigger responders by default")
    ok((await db.get("tickets", tid))["staff_replied"] == 1, "staff message recorded")
    await db.update("autoresponders", rid, {"staff_triggers": 1, "cooldown": 0})
    await autoresponder.handle_message(bot, msg("price", who=staff))
    ok(len(tch.sent) == 3, "staff can trigger when allowed; cooldown 0 means no wait")

    # ---- scope: server-wide, optionally one channel; empty responders are skipped
    await db.insert("autoresponders", {"guild_id": 1, "name": "rules", "trigger": "!rules", "match_type": "starts", "response": "read the rules",
                                       "scope": "server", "channel_id": 101, "cooldown": 0, "reply": 0})
    await autoresponder.handle_message(bot, msg("!rules", ch=gen))
    ok(len(gen.sent) == 1 and gen.sent[0]["reference"] is None, "server scope answers outside tickets (plain send)")
    await autoresponder.handle_message(bot, msg("!rules", ch=other))
    ok(not other.sent, "channel restriction respected")
    await autoresponder.handle_message(bot, msg("price", ch=gen))
    ok(len(gen.sent) == 1, "ticket-scope responders stay out of normal channels")
    await db.insert("autoresponders", {"guild_id": 1, "name": "empty", "trigger": "blank", "scope": "server", "cooldown": 0})
    await autoresponder.handle_message(bot, msg("blank", ch=gen))
    ok(len(gen.sent) == 1, "a responder with nothing to say sends nothing")

    # ---- panel / card buttons: link, reply, role, and a half-configured button
    pid = await db.insert("panels", {"guild_id": 1, "name": "Card", "title": "Hello", "channel_id": 101})
    b_link = await db.insert("panel_buttons", {"panel_id": pid, "label": "Docs", "action": "link", "url": "https://example.com/docs", "position": 0})
    b_reply = await db.insert("panel_buttons", {"panel_id": pid, "label": "FAQ", "action": "reply", "reply_text": "answers", "position": 1})
    role = discord.Role(10, "Staff"); role.managed = False; role.permissions = NS(administrator=False)
    guild.roles = {10: role}
    b_role = await db.insert("panel_buttons", {"panel_id": pid, "label": "Updates", "action": "role", "role_id": 10, "position": 2})
    b_bad = await db.insert("panel_buttons", {"panel_id": pid, "label": "Open", "action": "create", "position": 3})   # no category
    embeds, view = await panels.render_panel(bot, await db.get("panels", pid))
    by = {c.label: c for c in view.children}
    ok(by["Docs"].url == "https://example.com/docs" and by["Docs"].custom_id is None, "panel link button")
    ok(by["FAQ"].custom_id == f"tb:b:{b_reply}", "panel reply button keeps a stable custom id")

    i = FakeInteraction(member, guild, gen); await by["FAQ"].callback(i)
    ok(i.response.log[0][2]["content"] == "answers" and i.response.log[0][2]["ephemeral"], "panel reply button answers privately")
    member.add_roles = lambda r, reason=None: _add(member, r); member.remove_roles = lambda r, reason=None: _rm(member, r)
    async def _add(m, r): m.roles.append(r)
    async def _rm(m, r): m.roles.remove(r)
    i = FakeInteraction(member, guild, gen); await by["Updates"].callback(i)
    ok(role in member.roles and "now have" in i.response.log[0][1][0], "role button gives the role")
    i = FakeInteraction(member, guild, gen); await by["Updates"].callback(i)
    ok(role not in member.roles and "Removed" in i.response.log[0][1][0], "role button takes it away again")
    role.permissions = NS(administrator=True)
    i = FakeInteraction(member, guild, gen); await by["Updates"].callback(i)
    ok(role not in member.roles and "can't be handed out" in i.response.log[0][1][0], "administrator roles are refused")
    role.permissions = NS(administrator=False)
    async def _forbid(r, reason=None): raise discord.Forbidden()
    member.add_roles = _forbid
    i = FakeInteraction(member, guild, gen); await by["Updates"].callback(i)
    ok("Manage Roles" in i.response.log[0][1][0], "missing permission is explained")
    i = FakeInteraction(member, guild, gen); await by["Open"].callback(i)
    ok("isn't set up" in i.response.log[0][1][0], "half-configured button says so instead of failing")
    ok(actions.button_problem(await db.get("panel_buttons", b_bad)) and not actions.button_problem(await db.get("panel_buttons", b_link)), "button_problem")

    # ---- dashboard: sections, create flow, buttons child, preview, role picker
    for key in ("autoresponders", "automation"):
        scr = await crud.section_screen(bot, 1, 1, key)
        ok(scr["embeds"], f"dashboard section {key} renders")
    ar = E["autoresponder"]; first = next(iter(ar.groups))
    modal = crud.RowModal(bot, ar, first, None, None, 1)
    modal.inputs["name"].value, modal.inputs["trigger"].value = "hours", "hours, open"
    modal.inputs["match_type"].value, modal.inputs["response"].value = "", "we are open 9-5"
    i = FakeInteraction(staff, guild, gen); i.message = None; await modal.on_submit(i)
    new = await db.fetchone("SELECT * FROM autoresponders WHERE name = 'hours'")
    ok(new and new["match_type"] == "contains" and new["scope"] == "tickets" and new["cooldown"] == 60, "create modal: defaults applied")
    mbtn = crud.crud_screen  # child list for buttons of the new responder
    scr = await mbtn(bot, 1, 1, E["ar_button"], parent_id=new["id"])
    ok(scr["embeds"], "auto-responder buttons screen renders")
    bm = crud.RowModal(bot, E["ar_button"], "Button", None, new["id"], 1)
    bm.inputs["label"].value, bm.inputs["style"].value, bm.inputs["action"].value, bm.inputs["emoji"].value = "Call us", "success", "link", ""
    i = FakeInteraction(staff, guild, gen); i.message = None; await bm.on_submit(i)
    nb = await db.fetchone("SELECT * FROM autoresponder_buttons WHERE label = 'Call us'")
    ok(nb and nb["action"] == "link" and nb["responder_id"] == new["id"], "button created under the responder")
    desc = await crud.describe(bot, E["ar_button"], nb)
    ok(any(f[0] == "Needs attention" for f in desc.fields), "describe warns that a link button has no URL")
    um = crud.RowModal(bot, E["ar_button"], "Link & reply", nb, new["id"], 1)
    um.inputs["url"].value = "https://example.com/call"
    i = FakeInteraction(staff, guild, gen); i.message = None; await um.on_submit(i)
    ok((await db.get("autoresponder_buttons", nb["id"]))["url"] == "https://example.com/call", "link URL saved via the second modal group")
    row = await db.get("autoresponder_buttons", nb["id"])
    pick = await crud.picker_screen(bot, 1, 1, E["ar_button"], row, next(p for p in E["ar_button"].pickers if p.kind == "role"))
    ok(pick["view"].children, "single-role picker renders")
    ok(await crud.picker_display(bot, next(p for p in E["ar_button"].pickers if p.kind == "role"), 10) == "<@&10>", "role picker display")
    dr = await crud.edit_screen(bot, 1, 1, E["automation"], 1)
    ok(dr["view"].children, "automation edit screen renders")

    # ---- automation timers use the configured messages
    old = (discord.utils.utcnow() - timedelta(minutes=30)).isoformat()
    t4 = await db.insert("tickets", {"guild_id": 1, "number": 4, "channel_id": 400, "owner_id": 6, "category_id": cat, "status": "open", "created_at": old})
    ch4 = FakeChannel(400, "ticket-0004", guild); guild.channels[400] = ch4
    await automation.watch_once(bot)
    ok(not ch4.sent, "timers are off by default")
    await db.update("guild_settings", 1, {"unclaimed_minutes": 15, "nudge_message": "Hang tight {user}, ticket {number}"})
    await automation.watch_once(bot)
    ok(len(ch4.sent) == 1 and ch4.sent[0]["embed"].description == "Hang tight <@6>, ticket 0004", "custom nudge message")
    await automation.watch_once(bot)
    ok(len(ch4.sent) == 1, "nudge is sent only once")
    await db.update("guild_settings", 1, {"idle_hours": 1, "autoclose_hours": 1, "idle_message": None})
    await db.update("tickets", t4, {"last_activity": (discord.utils.utcnow() - timedelta(hours=2)).isoformat(), "staff_replied": 1})
    await automation.watch_once(bot)
    ok(len(ch4.sent) == 2 and "Still there" in ch4.sent[1]["embed"].description and "close automatically" in ch4.sent[1]["embed"].description, "default idle reminder + auto-close notice")

    # ---- legacy migration: old shop database keeps its timers and keyword replies
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    import sqlite3
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE guild_settings (id INTEGER PRIMARY KEY, max_open_tickets INTEGER NOT NULL DEFAULT 3, prevent_duplicates INTEGER NOT NULL DEFAULT 1,
        naming_format TEXT NOT NULL DEFAULT 'ticket-{number}', staff_role_ids TEXT NOT NULL DEFAULT '[]', admin_role_ids TEXT NOT NULL DEFAULT '[]',
        ticket_category_id INTEGER, welcome_message TEXT NOT NULL DEFAULT 'hi', close_confirmation INTEGER NOT NULL DEFAULT 1,
        auto_transcript INTEGER NOT NULL DEFAULT 1, archive_channel_id INTEGER, log_channel_id INTEGER, ticket_counter INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE panels (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL);
      CREATE TABLE panel_buttons (id INTEGER PRIMARY KEY AUTOINCREMENT, panel_id INTEGER NOT NULL, label TEXT NOT NULL, emoji TEXT,
        style TEXT NOT NULL DEFAULT 'primary', action TEXT NOT NULL DEFAULT 'create', category_id INTEGER, form_id INTEGER,
        enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, number INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
      CREATE TABLE shop_settings (guild_id INTEGER PRIMARY KEY, unclaimed_minutes INTEGER, idle_hours INTEGER, autoclose_hours INTEGER,
        autoreply_enabled INTEGER, autoreply_when_claimed INTEGER);
      CREATE TABLE autoreplies (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, trigger TEXT NOT NULL, response TEXT NOT NULL,
        match_type TEXT NOT NULL DEFAULT 'contains', enabled INTEGER NOT NULL DEFAULT 1);
      INSERT INTO guild_settings (id) VALUES (7);
      INSERT INTO panels (guild_id, name) VALUES (7, 'old');
      INSERT INTO panel_buttons (panel_id, label) VALUES (1, 'Order');
      INSERT INTO shop_settings VALUES (7, 15, 24, 6, 1, 1);
      INSERT INTO autoreplies (guild_id, trigger, response, match_type, enabled) VALUES (7, 'price, magkano', 'see {shop}', 'exact', 0);
    """)
    c.commit(); c.close()
    odb = Database(path); await odb.connect()
    gs = await odb.get("guild_settings", 7)
    ok((gs["unclaimed_minutes"], gs["idle_hours"], gs["autoclose_hours"], gs["autoreply_enabled"]) == (15, 24, 6, 1), "migration: timers copied")
    ars = await odb.fetchall("SELECT * FROM autoresponders")
    ok(len(ars) == 1 and ars[0]["trigger"] == "price, magkano" and ars[0]["match_type"] == "exact" and ars[0]["enabled"] == 0
       and ars[0]["when_claimed"] == 1 and ars[0]["scope"] == "tickets", "migration: keyword replies carried over")
    pb = await odb.fetchone("SELECT * FROM panel_buttons")
    ok(pb["label"] == "Order" and pb["ephemeral"] == 1 and pb["url"] is None, "migration: old buttons gain the new columns")
    await odb.execute("UPDATE guild_settings SET idle_hours = 3 WHERE id = 7")
    await odb.close(); odb = Database(path); await odb.connect()
    ok((await odb.get("guild_settings", 7))["idle_hours"] == 3 and len(await odb.fetchall("SELECT * FROM autoresponders")) == 1,
       "migration runs only once (later edits and duplicates are safe)")

    print("\nRESULT:", "FAILED" if ok.failed else "ALL PASSED")
    return 1 if ok.failed else 0

sys.exit(asyncio.run(main()))
