"""Offline regression test: runs the bot logic against fake discord/aiosqlite stand-ins (tests/stubs.py).
No network or token needed:  python tests/offline_smoke_test.py"""
import asyncio, os, sys, tempfile, json, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stubs
import discord
from types import SimpleNamespace as NS

os.environ["TRANSCRIPT_DIR"] = tempfile.mkdtemp()
from ticketbot.db import Database
from ticketbot import crud, panels, tickets, transcripts, util
from ticketbot.entities import ENTITIES
from ticketbot.commands import build_group
import main as mainmod   # import-time check of entrypoint

# ---------- fakes ----------
class FakeResponse:
    def __init__(s): s.done = False; s.log = []
    def is_done(s): return s.done
    async def send_message(s, *a, **k): s.done = True; s.log.append(("send", a, k))
    async def edit_message(s, **k): s.done = True; s.log.append(("edit", (), k))
    async def send_modal(s, m): s.done = True; s.log.append(("modal", (m,), {}))
    async def defer(s, **k): s.done = True; s.log.append(("defer", (), k))
class FakeMember(discord.Member):
    def __init__(s, id, guild, admin=False, roles=(), bot=False, name=None):
        s.id, s.guild, s.bot, s.roles = id, guild, bot, list(roles)
        s.name = name or f"user{id}"; s.display_name = s.name; s.mention = f"<@{id}>"
        s.guild_permissions = NS(administrator=admin)
        s.display_avatar = NS(url="https://x/a.png"); s.color = NS(value=0); s.dms = []
    async def send(s, *a, **k): s.dms.append((a, k))
class FakeMsg:
    _n = 1000
    def __init__(s, **k):
        FakeMsg._n += 1; s.id = FakeMsg._n; s.__dict__.update(k)
    async def edit(s, **k): s.edited = k
    async def delete(s): s.deleted = True
    async def pin(s): s.pinned = True
class FakeChannel(discord.TextChannel, discord.abc.Messageable):
    def __init__(s, id, name="chan", guild=None):
        s.id, s.name, s.guild, s.sent, s.perms = id, name, guild, [], {}
        s.mention = f"<#{id}>"
    async def send(s, *a, **k):
        m = FakeMsg(**k); s.sent.append((a, k)); return m
    def get_partial_message(s, mid): return FakeMsg(id=mid)
    async def set_permissions(s, target, overwrite=None, **k): s.perms[target.id] = overwrite
    async def edit(s, **k): s.name = k.get("name", s.name)
    async def delete(s, **k): s.deleted = True
    async def history(s, **k): 
        for m in s._history: yield m
    def history_(s): pass
class FakeGuild(discord.Guild):
    def __init__(s):
        s.id = 1; s.name = "Guild"; s.owner_id = 999; s.default_role = discord.Role(1, "@everyone")
        s.roles = {10: discord.Role(10, "Staff")}; s.channels = {}; s.members = {}
        s.me = FakeMember(500, s, admin=True)
    def get_channel(s, i): return s.channels.get(i)
    def get_role(s, i): return s.roles.get(i)
    def get_member(s, i): return s.members.get(i)
    async def create_text_channel(s, name, **k):
        ch = FakeChannel(7000 + len(s.channels), name, s); ch.kwargs = k; s.channels[ch.id] = ch; return ch
class FakeInteraction:
    def __init__(s, user, guild, message=None, channel=None):
        s.user, s.guild, s.guild_id, s.message = user, guild, guild.id, message
        s.response, s.channel, s.channel_id = FakeResponse(), channel, getattr(channel, "id", None)
        s.followups = []
        s.followup = NS(send=s._fu)
    async def _fu(s, *a, **k): s.followups.append((a, k))
    async def edit_original_response(s, **k): s.response.log.append(("edit_orig", (), k))

def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: ok.failed = True
ok.failed = False

async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db")); await db.connect()
    guild = FakeGuild()
    admin = FakeMember(1, guild, admin=True); guild.members[1] = admin
    bot = NS(db=db, user_locks={}, channels={}, views=[], tree=discord.app_commands.Group("tree", ""))
    bot.get_channel = lambda i: guild.channels.get(i)
    bot.get_guild = lambda i: guild
    bot.add_view = lambda v, message_id=None: bot.views.append((v, message_id))
    dest = FakeChannel(42, "panel-channel", guild); guild.channels[42] = dest

    ok(bool(build_group(bot)), "slash command tree builds")

    # ---- dashboard + section screens
    s = await crud.dashboard_screen(bot, 1, 1); ok(len(s["view"].children) == 1, "dashboard renders")
    for key, *_ in crud.SECTIONS:
        scr = await crud.section_screen(bot, 1, 1, key)
        ok("view" in scr, f"section '{key}' renders")

    # ---- helper to submit a modal
    async def submit(entity, group, row, parent_id, values):
        m = crud.RowModal(bot, entity, group, row, parent_id, 1)
        for k, v in values.items(): m.inputs[k].value = v
        i = FakeInteraction(admin, guild, message=object())
        await m.on_submit(i); return i

    E = ENTITIES
    i = await submit(E["category"], "Main", None, None, {"name": "Order Support", "description": "orders",
                      "naming_format": "{category}-{number}", "welcome_message": "Hi {user}, {category} #{number}"})
    cat = (await db.where("categories", guild_id=1))[0]
    ok(cat["name"] == "Order Support", "create category via modal")
    i = await submit(E["category"], "Main", None, None, {"name": "Order Support"})
    ok("already exists" in i.response.log[0][1][0], "duplicate name rejected")

    # form + questions
    await submit(E["form"], "Form", None, None, {"name": "Order Form"})
    form = (await db.where("forms", guild_id=1))[0]
    for q in ("What did you buy?", "Order ID", "Describe", "q4", "q5"):
        await submit(E["question"], "Question", None, form["id"], {"label": q, "placeholder": "", "style": "paragraph" if q == "Describe" else "short"})
    ok(len(await db.where("form_questions", form_id=form["id"])) == 5, "5 form questions created")
    # picker: link form to category
    scr = await crud.picker_screen(bot, 1, 1, E["category"], cat, [p for p in E["category"].pickers if p.key == "form_id"][0])
    comp = scr["view"].children[0]; comp.values = [str(form["id"])]
    j = FakeInteraction(admin, guild, message=object()); await comp.callback(j)
    cat = await db.get("categories", cat["id"]); ok(cat["form_id"] == form["id"], "ref picker saves form on category")
    # roles picker
    p = [p for p in E["category"].pickers if p.key == "staff_role_ids"][0]
    scr = await crud.picker_screen(bot, 1, 1, E["category"], cat, p)
    comp = scr["view"].children[0]; comp.values = [guild.roles[10]]
    await comp.callback(FakeInteraction(admin, guild, message=object()))
    cat = await db.get("categories", cat["id"]); ok(json.loads(cat["staff_role_ids"]) == [10], "role picker saves roles")

    # embed template + fields
    await submit(E["embed"], "Main", None, None, {"name": "Support", "title": "Need help?", "description": "Pick below", "color": "#FF0000"})
    emb = (await db.where("embed_templates", guild_id=1))[0]
    ok(emb["color"] == 0xFF0000, "color parsed")
    await submit(E["embed_field"], "Field", None, emb["id"], {"name": "Hours", "value": "24/7"})
    await submit(E["embed_field"], "Field", None, emb["id"], {"name": "Reply", "value": "fast"})
    f = await db.where("embed_fields", order="position", embed_id=emb["id"]); ok([x["position"] for x in f] == [0, 1], "fields ordered")
    i = await submit(E["embed"], "Main", emb, None, {"name": "Support", "title": "x", "description": "", "color": "nope"})
    ok("hex" in i.response.log[0][1][0], "bad color rejected")

    # panel + buttons + dropdown
    await submit(E["panel"], "Basics", None, None, {"name": "Main", "title": "Tickets", "description": "d", "color": ""})
    panel = (await db.where("panels", guild_id=1))[0]
    ps = [p for p in E["panel"].pickers if p.key == "embed_ids"][0]
    scr = await crud.picker_screen(bot, 1, 1, E["panel"], panel, ps); comp = scr["view"].children[0]; comp.values = [str(emb["id"])]
    await comp.callback(FakeInteraction(admin, guild, message=object()))
    ps = [p for p in E["panel"].pickers if p.key == "channel_id"][0]
    scr = await crud.picker_screen(bot, 1, 1, E["panel"], panel, ps); comp = scr["view"].children[0]; comp.values = [NS(id=42)]
    await comp.callback(FakeInteraction(admin, guild, message=object()))
    await submit(E["button"], "Button", None, panel["id"], {"label": "Open", "emoji": "🎫", "style": "success", "action": "create"})
    btn = (await db.where("panel_buttons", panel_id=panel["id"]))[0]
    await db.update("panel_buttons", btn["id"], {"category_id": cat["id"]})
    await submit(E["dropdown"], "Dropdown", None, panel["id"], {"placeholder": "Pick one", "min_values": "1", "max_values": "1"})
    dd = (await db.where("panel_dropdowns", panel_id=panel["id"]))[0]
    await submit(E["option"], "Option", None, dd["id"], {"label": "Order Support", "description": "orders", "emoji": "🛒"})
    opt = (await db.where("dropdown_options", dropdown_id=dd["id"]))[0]
    await db.update("dropdown_options", opt["id"], {"category_id": cat["id"]})
    i = await submit(E["button"], "Button", None, panel["id"], {"label": "x", "emoji": "abc", "style": "success", "action": "create"})
    ok("emoji" in i.response.log[0][1][0].lower(), "invalid emoji rejected")

    # render / preview / publish
    panel = await db.get("panels", panel["id"])
    embeds, view = await panels.render_panel(bot, panel, preview=True)
    ok(len(embeds) == 1 and embeds[0].title == "Need help?" and len(embeds[0].fields) == 2, "panel renders template embed with fields")
    ok(len(view.children) == 2 and view.children[0].row == 0 and view.children[1].row == 1, "dropdown row 0, buttons row 1")
    msg = await panels.publish(bot, guild, panel["id"]); ok("published" in msg, "panel published: " + msg)
    panel = await db.get("panels", panel["id"]); ok(panel["message_id"] is not None, "message id stored")
    ok(any(mid == panel["message_id"] for _, mid in bot.views), "persistent view registered")
    # live sync: edit embed -> published message edited
    n = len(dest.sent)
    await submit(E["embed"], "Main", emb, None, {"name": "Support", "title": "New title", "description": "x", "color": "#00FF00"})
    ok(True, "edit embed with published panel did not raise")
    # toggle button off -> still ok
    view_obj = (await crud.crud_screen(bot, 1, 1, E["button"], parent_id=panel["id"], selected_id=btn["id"]))["view"]
    tgl = [c for c in view_obj.children if getattr(c, "label", "").endswith(("Disable", "Enable"))][0]
    await tgl.callback(FakeInteraction(admin, guild, message=object()))
    ok((await db.get("panel_buttons", btn["id"]))["enabled"] == 0, "toggle disables button")
    await db.update("panel_buttons", btn["id"], {"enabled": 1})
    # move
    await submit(E["button"], "Button", None, panel["id"], {"label": "Second", "emoji": "", "style": "primary", "action": "create"})
    v = (await crud.crud_screen(bot, 1, 1, E["button"], parent_id=panel["id"], selected_id=btn["id"]))["view"]
    down = [c for c in v.children if getattr(c, "label", "").endswith("Down")][0]
    await down.callback(FakeInteraction(admin, guild, message=object()))
    labels = [r["label"] for r in await db.where("panel_buttons", order="position", panel_id=panel["id"])]
    ok(labels == ["Second", "Open"], "move down reorders buttons")
    # too many components
    for k in range(25): await db.insert("panel_buttons", {"panel_id": panel["id"], "label": f"b{k}", "position": 10 + k})
    try: await panels.render_panel(bot, await db.get("panels", panel["id"])); ok(False, "row overflow raises")
    except ValueError: ok(True, "row overflow raises ValueError")
    await db.delete("panel_buttons", (await db.where("panel_buttons", panel_id=panel["id"]))[-1]["id"])
    await db.execute("DELETE FROM panel_buttons WHERE label LIKE 'b%'")

    # ---- ticket creation
    user = FakeMember(2, guild, name="Alice"); guild.members[2] = user
    inter = FakeInteraction(user, guild, channel=dest)
    await tickets.handle_entry(inter, bot, category_id=cat["id"], action="create", form_id=None, panel_id=panel["id"])
    ok(inter.response.log[0][0] == "modal", "category with form -> opens modal")
    modal = inter.response.log[0][1][0]; ok(len(modal.children) == 5, "form modal has 5 inputs")
    for ti, v in zip(modal.children, ["Bot", "A1", "Help me", "x", "y"]): ti.value = v
    inter2 = FakeInteraction(user, guild, channel=dest)
    await modal.on_submit(inter2)
    t = (await db.where("tickets", guild_id=1))[0]
    ch = guild.channels[t["channel_id"]]
    ok(t["number"] == 1 and ch.name == "order-support-0001", f"ticket created, channel name {ch.name}")
    ok(10 in {getattr(k, 'id', None) for k in ch.kwargs["overwrites"]}, "staff role overwrite set")
    ok(inter2.followups and "created" in inter2.followups[0][0][0], "owner notified")
    ok(len(ch.sent[0][1]["embed"].fields) == 0, "welcome message carries no form fields")
    ok(len(ch.sent) >= 2 and len(ch.sent[1][1]["embed"].fields) == 5 and "view" not in ch.sent[1][1],
       "form answers sent as a separate message")
    # duplicate prevention
    inter3 = FakeInteraction(user, guild, channel=dest)
    await tickets.handle_entry(inter3, bot, category_id=cat["id"], action="category", form_id=None, panel_id=panel["id"])
    ok("already have an open ticket" in inter3.response.log[0][1][0], "duplicate ticket blocked")
    # max open
    await db.update("guild_settings", 1, {"max_open_tickets": 1, "prevent_duplicates": 0})
    inter4 = FakeInteraction(user, guild, channel=dest)
    await tickets.handle_entry(inter4, bot, category_id=cat["id"], action="category", form_id=None, panel_id=panel["id"])
    ok("maximum" in inter4.response.log[0][1][0], "max open tickets enforced")

    # ---- management
    staff = FakeMember(3, guild, roles=[guild.roles[10]], name="Bob"); guild.members[3] = staff
    guild.channels[t["channel_id"]].id = t["channel_id"]
    ctl = tickets.TicketControlView(bot)
    ci = FakeInteraction(staff, guild, channel=ch); await ctl.claim.callback(ci)
    ok((await db.get("tickets", t["id"]))["claimed_by"] == 3, "claim")
    ci = FakeInteraction(user, guild, channel=ch); await ctl.claim.callback(ci)
    ok("Only ticket staff" in ci.response.log[0][1][0], "non-staff can't claim")
    ci = FakeInteraction(staff, guild, channel=ch); await ctl.unclaim.callback(ci)
    ok((await db.get("tickets", t["id"]))["claimed_by"] is None, "unclaim")
    # rename keeps number
    rm = tickets.RenameModal(bot, ch.name); rm.name.value = "billing-help"
    ci = FakeInteraction(staff, guild, channel=ch); ci.followup = NS(send=ci._fu); await rm.on_submit(ci)
    ok(ch.name == "billing-help-0001", f"rename preserves number -> {ch.name}")
    # add user
    upv = tickets.UserPickView(bot, "add", 3); carol = FakeMember(4, guild, name="Carol"); guild.members[4] = carol
    upv.select.values = [carol]; ci = FakeInteraction(staff, guild, channel=ch); await upv.picked(ci)
    ok(len(await db.where("ticket_participants", ticket_id=t["id"])) == 1, "user added")
    upv = tickets.UserPickView(bot, "remove", 3); upv.select.values = [carol]; await upv.picked(FakeInteraction(staff, guild, channel=ch))
    ok(len(await db.where("ticket_participants", ticket_id=t["id"])) == 0, "user removed")
    # transfer
    tv = tickets.TransferView(bot, t["id"], await db.where("categories", guild_id=1), 3); tv.member = staff
    await tv.confirm.callback(FakeInteraction(staff, guild, channel=ch))
    ok((await db.get("tickets", t["id"]))["claimed_by"] == 3, "transfer sets assignee")
    # close (with transcript)
    msgs = [NS(author=user, type=discord.MessageType.default, created_at=discord.utils.utcnow(), clean_content="hello **world** <b>x</b>",
               embeds=[], attachments=[], system_content="")]
    async def hist(**k):
        for m in msgs: yield m
    ch.history = hist
    await db.update("guild_settings", 1, {"archive_channel_id": 42, "log_channel_id": 42})
    await tickets.close_ticket(bot, ch, t["id"], staff)
    tt = await db.get("tickets", t["id"]); ok(tt["status"] == "closed" and tt["closed_by"] == 3, "ticket closed")
    tr = await db.fetchall("SELECT * FROM transcripts"); ok(len(tr) == 1 and os.path.exists(tr[0]["file_path"]), "transcript saved")
    html_text = open(tr[0]["file_path"]).read()
    ok("hello <b>world</b>" in html_text and "&lt;b&gt;x&lt;/b&gt;" in html_text, "transcript escapes + formats markdown")
    ok("Ticket Closed" in html_text and "Order ID" in html_text, "transcript has activity log + form answers")
    ok(any("Transcript" in (a[1].get("embed").title if a[1].get("embed") else "") for a in dest.sent), "transcript posted to archive channel")
    ok(any(k == 2 for k in ch.perms), "owner restricted on close")
    # ---- vouch: request after close, one vouch per ticket
    owner = guild.members.get(2) or FakeMember(2, guild); guild.members[2] = owner
    vch = FakeChannel(77, "vouches", guild); guild.channels[77] = vch
    await db.update("guild_settings", 1, {"vouch_channel_id": 77})
    prompts = [a for a in ch.sent if a[1].get("view") is not None and a[1].get("embed") is not None
               and "How did we do" in (a[1]["embed"].title or "")]
    ok(len(prompts) == 1, "vouch request sent as its own message on close")
    ok(owner.dms and "How did we do" in owner.dms[-1][1]["embed"].title, "vouch request also sent by DM")
    from ticketbot import vouch as V
    tkt = await db.get("tickets", t["id"])
    btn = V.VouchButton(tkt["id"])
    stranger = FakeMember(55, guild)
    bi = FakeInteraction(stranger, guild, channel=ch); bi.client = bot
    await V.VouchButton.callback(btn, bi)
    ok("Only the ticket owner" in bi.response.log[0][1][0], "only the ticket owner can vouch")
    bi = FakeInteraction(owner, guild, channel=ch); bi.client = bot
    await V.VouchButton.callback(btn, bi)
    ok(bi.response.log[0][0] == "modal" and len(bi.response.log[0][1][0].children) == 5, "vouch button opens 5-part form")
    vm = bi.response.log[0][1][0]
    ok(isinstance(vm.rating, discord.ui.Select) and len(vm.rating.options) == 5, "rating dropdown has 5 choices")
    vm.rating.values = ["5"]; vm.item.value = "Logo"; vm.comment.value = "so good"
    vm.staff.values = []; vm.image.values = []
    si = FakeInteraction(owner, guild, channel=ch); si.client = bot
    await vm.on_submit(si)
    row = await db.fetchone("SELECT * FROM ticket_vouches WHERE ticket_id = ?", (tkt["id"],))
    ok(row and row["rating"] == 5 and row["item"] == "Logo", "vouch saved")
    ok(vch.sent and "New vouch" in vch.sent[-1][1]["embed"].title, "vouch posted in the vouch channel")
    bi = FakeInteraction(owner, guild, channel=ch); bi.client = bot
    await V.VouchButton.callback(btn, bi)
    ok("already vouched" in bi.response.log[0][1][0], "second vouch refused")
    # reopen
    closed = tickets.ClosedView(bot); ci = FakeInteraction(staff, guild, channel=ch, message=object()); await closed.reopen.callback(ci)
    ok((await db.get("tickets", t["id"]))["status"] == "open", "reopen")
    # delete (transcript already exists)
    ci = FakeInteraction(staff, guild, channel=ch, message=object())
    import ticketbot.tickets as T
    orig = T.asyncio.sleep; T.asyncio.sleep = lambda s: orig(0)
    await tickets.delete_ticket(bot, ch, t["id"], staff, ci)
    ok((await db.get("tickets", t["id"]))["status"] == "deleted" and getattr(ch, "deleted", False), "delete keeps record, removes channel")
    logs = [r["action"] for r in await db.where("ticket_logs", ticket_id=t["id"])]
    ok(all(a in logs for a in ["ticket_created", "ticket_claimed", "ticket_unclaimed", "ticket_renamed", "user_added",
                               "user_removed", "ticket_transferred", "ticket_closed", "ticket_reopened", "ticket_deleted"]), "all 10 log events recorded")

    # ---- deleting a panel cascades + removes message
    scr = await crud.crud_screen(bot, 1, 1, E["panel"], selected_id=panel["id"])
    await crud.delete_row(bot, E["panel"], await db.get("panels", panel["id"]))
    ok(await db.where("panel_buttons", panel_id=panel["id"]) == [], "panel delete cascades to buttons")
    ok((await db.get("categories", cat["id"])) is not None, "category survives panel delete")
    await crud.delete_row(bot, E["form"], form)
    ok((await db.get("categories", cat["id"]))["form_id"] is None, "deleting form nulls category link")

    # ---- /ticket quickpanel (one-form setup, re-run edits in place)
    from ticketbot import quick
    ok(quick.parse_topics("A | desc | 🛒\nB")[0][1]["label"] == "B", "quick: topics parse")
    ok(quick.parse_topics("A\nA")[1] is not None, "quick: duplicate topic rejected")
    ok(quick.parse_topics("A | d | ♡ | Q1 ; Q2+")[0][0]["questions"] == [("Q1", False), ("Q2", True)], "quick: questions parse")
    ok(quick.parse_topics("A | d | ♡ | 1;2;3;4;5;6")[1] is not None, "quick: >5 questions rejected")
    qch = FakeChannel(77, "buy", guild); guild.channels[77] = qch
    staff = guild.roles[10]
    async def run_quick(values, banner=None):
        old = await quick._existing(bot, 1, 77)
        imgs = [{"url": banner, "kind": "image"}] if banner else (old.get("images") or [])
        m = quick.QuickPanelModal(bot, qch, images=imgs, thumbnail="https://x/t.png", staff_role=staff,
                                  ticket_folder=None, old=old)
        for k, v in values.items(): getattr(m, k).value = v
        i = FakeInteraction(admin, guild, message=object()); await m.on_submit(i); return i
    i = await run_quick({"f_text": "**hi**", "f_bar": "bar", "f_icon": "", "f_placeholder": "Select a topic...",
                         "f_topics": "Order | Buy | 🛒\nReport | Problem | 🚩"}, banner="https://x/b.png")
    qp = await db.fetchone("SELECT * FROM panels WHERE name = ?", ("quick-77",))
    ok(qp is not None and len(json.loads(qp["embed_ids"])) == 1 and len(json.loads(qp["image_message_ids"])) == 1, "quick: panel (bar embed) + gallery message created")
    ok(len(qch.sent) == 2 and len(qch.sent[0][1]["embeds"]) == 1 and "view" not in qch.sent[0][1], "quick: banner posted first")
    ok(qch.sent[0][1]["embeds"][0]._image == "https://x/b.png" and qch.sent[0][1]["embeds"][0]._thumb == "https://x/t.png", "quick: first picture carries the thumbnail")
    ok("view" in qch.sent[1][1] and qch.sent[1][1].get("content") == "**hi**", "quick: text + bar + dropdown posted second")
    ok(len(await db.fetchall("SELECT * FROM dropdown_options o JOIN panel_dropdowns d ON d.id=o.dropdown_id WHERE d.panel_id=?", (qp["id"],))) == 2, "quick: 2 options")
    ok(i.followups and "published" in i.followups[0][0][0], "quick: success message sent")
    old = await quick._existing(bot, 1, 77)
    m2 = quick.QuickPanelModal(bot, qch, images=[], thumbnail=None, staff_role=None, ticket_folder=None, old=old)
    ok(m2.f_topics.default == "Order | Buy | 🛒\nReport | Problem | 🚩" and m2.f_text.default == "**hi**", "quick: form pre-filled on re-run")
    await run_quick({"f_text": "**changed**", "f_bar": "", "f_icon": "", "f_placeholder": "Pick", "f_topics": "Order | Buy | 🛒"}, banner="https://x/b.png")
    ok(len(await db.fetchall("SELECT * FROM panels WHERE name = ?", ("quick-77",))) == 1, "quick: re-run replaces, no duplicate panel")
    ok(len(await db.fetchall("SELECT * FROM categories WHERE name = ? AND guild_id = 1", ("Order",))) == 1, "quick: category reused")
    s2 = await db.get("guild_settings", 1)
    ok(10 in json.loads(s2["staff_role_ids"]), "quick: staff role saved")
    i = await run_quick({"f_text": "x", "f_bar": "", "f_icon": "nope", "f_placeholder": "p", "f_topics": "A"})
    ok("icon" in i.response.log[0][1][0], "quick: bad icon link rejected")

    # =====================================================================
    # /ticket help + admin / staff commands (admin.py)
    # =====================================================================
    from ticketbot import admin as adm
    cmds = {**bot.tree.cmds, **build_group(bot).cmds}

    def said(i):
        out = []
        for _, a, k in i.response.log:
            out.append(str(a[0]) if a and isinstance(a[0], str) else str(k.get("content") or ""))
            e = k.get("embed")
            if e is not None:
                out.append(f"{e.title}\n{e.description}\n" + "\n".join(f"{n}: {v}" for n, v, _ in e.fields))
        for a, k in i.followups:
            out.append(str(a[0]) if a else str(k.get("content") or ""))
        return "\n".join(out)

    def embed_of(i):
        return next(k["embed"] for _, a, k in i.response.log if k.get("embed") is not None)

    eve = FakeMember(60, guild, name="Eve"); guild.members[60] = eve              # plain member
    bob = guild.members[3]                                                         # staff (role 10)
    alice = guild.members[2]                                                       # ticket owner

    # ---- registration sanity
    wanted = {"help", "dashboard", "quickpanel", "transcript", "panels", "publish", "unpublish",
              "settings", "list", "info", "close", "reopen", "delete", "post", "imageurl", "image"}
    ok(wanted <= set(cmds), "all commands registered: " + ", ".join(sorted(wanted - set(cmds))))
    img_cmds = cmds["image"].cmds
    ok(set(img_cmds) == {"add", "bulk", "list", "remove", "clear"}, "image group registered with 5 subcommands")
    all_fns = [f for f in cmds.values() if hasattr(f, "__desc__")] + list(img_cmds.values())
    ok(all(len(f.__desc__) <= 100 and f.__desc__ for f in all_fns), "command descriptions are 1-100 chars")
    ok(all(n == n.lower() and len(n) <= 32 for n in cmds), "command names are valid")

    # ---- /ticket help
    i = FakeInteraction(eve, guild); await cmds["help"](i)
    ok("Administrator" in said(i) and not any(k.get("embed") for _, _, k in i.response.log), "help: denied for non-admin")
    i = FakeInteraction(bob, guild); await cmds["help"](i)
    ok("Administrator" in said(i), "help: denied for staff who are not admins")
    i = FakeInteraction(admin, guild); await cmds["help"](i)
    he = embed_of(i)
    ok(i.response.log[0][2].get("ephemeral") is True, "help: ephemeral")
    total_chars = len(he.title) + len(he.description) + sum(len(n) + len(v) for n, v, _ in he.fields)
    ok(len(he.fields) <= 25 and total_chars < 6000 and all(len(v) <= 1024 and len(n) <= 256 for n, v, _ in he.fields),
       f"help: within Discord embed limits ({total_chars} chars)")
    help_text = he.description + " ".join(v for _, v, _ in he.fields)
    def documented(n): return f"/ticket {n}" in help_text or f"/{n}" in help_text or f"`{n}`" in help_text
    ok(all(documented(n) for n in wanted - {"help"}), "help: documents every command: "
       + ", ".join(n for n in wanted - {"help"} if not documented(n)))
    ok(total_chars < 3300, f"help: not verbose ({total_chars} chars)")

    # ---- admin gate on every admin command
    for name, kw in (("panels", {}), ("publish", {"panel": "1"}), ("unpublish", {"panel": "1"}),
                     ("settings", {}), ("list", {})):
        i = FakeInteraction(eve, guild); await cmds[name](i, **kw)
        ok("Administrator" in said(i), f"/{name}: denied for non-admin")
        i = FakeInteraction(bob, guild); await cmds[name](i, **kw)
        ok("Administrator" in said(i), f"/{name}: denied for staff-only member")

    # ---- /ticket panels
    i = FakeInteraction(admin, guild); await cmds["panels"](i)
    txt = said(i)
    ok("Quick panel · #buy" in txt and "posted" in txt and "<#77>" in txt, "panels: lists quick panel with friendly name + status")
    qp = await db.fetchone("SELECT * FROM panels WHERE name = ?", ("quick-77",))

    # message-deletion tracker for the channels we publish into
    deleted = []
    class _PM:
        def __init__(s, mid): s.id = mid
        async def delete(s): deleted.append(s.id)
    qch.get_partial_message = lambda mid: _PM(mid)
    dest.get_partial_message = lambda mid: _PM(mid)

    # ---- autocomplete
    ac_cb = cmds["publish"].__autocomplete__["panel"]
    ok(cmds["unpublish"].__autocomplete__["panel"] is ac_cb or callable(cmds["unpublish"].__autocomplete__["panel"]), "autocomplete wired on publish + unpublish")
    ch_all = await ac_cb(FakeInteraction(admin, guild), "")
    ok(any(c.name == "Quick panel · #buy" and c.value == str(qp["id"]) for c in ch_all), "autocomplete: friendly name, id value")
    ok([c.name for c in await ac_cb(FakeInteraction(admin, guild), "BUY")] == ["Quick panel · #buy"], "autocomplete: case-insensitive filter")
    ok(await ac_cb(FakeInteraction(admin, guild), "zzz") == [], "autocomplete: no match -> empty")
    ok(await ac_cb(FakeInteraction(eve, guild), "") == [], "autocomplete: hidden from non-admins")

    # ---- /ticket publish (quick panel: banner + panel, old ones replaced)
    old_msg, old_decor = qp["message_id"], json.loads(qp["image_message_ids"])
    n_before = len(qch.sent)
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(qp["id"]))
    qp2 = await db.get("panels", qp["id"])
    ok("published" in said(i), "publish: success message: " + said(i).strip())
    ok(len(qch.sent) == n_before + 2 and qp2["message_id"] != old_msg and json.loads(qp2["image_message_ids"]) != old_decor,
       "publish: posted new banner + panel, ids updated")
    ok(old_msg in deleted and all(d in deleted for d in old_decor), "publish: old panel AND old banner messages deleted")
    ok(qp2["message_id"] not in deleted and not set(json.loads(qp2["image_message_ids"])) & set(deleted), "publish: new messages kept")
    ok(qch.sent[-2][1]["embeds"] and "view" not in qch.sent[-2][1] and "view" in qch.sent[-1][1], "publish: banner first, then panel with dropdown")
    ok(any(mid == qp2["message_id"] for _, mid in bot.views), "publish: persistent view registered for new message")

    # name instead of id also resolves
    ok((await adm.resolve_panel(bot, guild, "Quick panel · #buy"))["id"] == qp["id"], "resolve: by friendly name")
    ok((await adm.resolve_panel(bot, guild, "quick-77"))["id"] == qp["id"], "resolve: by stored name")
    ok(await adm.resolve_panel(bot, guild, "nope") is None, "resolve: unknown -> None")

    # quick panel can't be moved
    n_before = len(dest.sent)
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(qp["id"]), channel=dest)
    ok("own channel" in said(i) and len(dest.sent) == n_before, "publish: quick panel can't be moved to another channel")
    # same channel explicitly is fine
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(qp["id"]), channel=qch)
    ok("published" in said(i), "publish: quick panel + its own channel is fine")

    # unknown panel
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel="does-not-exist")
    ok("couldn't find" in said(i), "publish: unknown panel handled")

    # failure while posting the banner leaves everything untouched
    qp3 = await db.get("panels", qp["id"]); real_send = qch.send
    async def boom(*a, **k): raise discord.HTTPException()
    qch.send = boom
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(qp["id"]))
    qch.send = real_send
    qp4 = await db.get("panels", qp["id"])
    ok("couldn't post" in said(i) and qp4["message_id"] == qp3["message_id"] and qp4["image_message_ids"] == qp3["image_message_ids"],
       "publish: banner failure leaves the live panel untouched")

    # ---- dashboard panel: publish to a channel, move it, failure keeps old message, disabled note
    pid = await db.insert("panels", {"guild_id": 1, "name": "Shop", "title": "Hello", "color": 0x123456,
                                     "embed_ids": "[]"})
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel="Shop")
    ok("no destination channel" in said(i), "publish: panel without channel asks for one")
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel="shop", channel=dest)
    shop = await db.get("panels", pid)
    ok("published" in said(i) and shop["message_id"] and shop["channel_id"] == 42 and shop["message_channel_id"] == 42,
       "publish: dashboard panel posted into chosen channel (saved as its destination)")
    first_msg = shop["message_id"]
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(pid))          # republish, no channel -> same one
    shop = await db.get("panels", pid)
    ok(shop["message_id"] != first_msg and first_msg in deleted, "publish: republish replaces the old message")
    real_send = dest.send
    dest.send = boom
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(pid))
    dest.send = real_send
    shop2 = await db.get("panels", pid)
    ok("rejected" in said(i) and shop2["message_id"] == shop["message_id"] and shop["message_id"] not in deleted,
       "publish: Discord failure keeps the old message")
    await db.update("panels", pid, {"enabled": 0})
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(pid))
    ok("disabled" in said(i), "publish: warns when the panel is disabled")
    await db.update("panels", pid, {"enabled": 1})

    # ---- /ticket panels again (two panels, one disabled flag etc.)
    i = FakeInteraction(admin, guild); await cmds["panels"](i)
    txt = said(i)
    ok("Shop" in txt and "Quick panel · #buy" in txt and "https://discord.com/channels/1/42/" in txt, "panels: lists both with jump link")

    # ---- /ticket unpublish
    shop = await db.get("panels", pid)
    i = FakeInteraction(admin, guild); await cmds["unpublish"](i, panel=str(pid))
    shop3 = await db.get("panels", pid)
    ok("unpublished" in said(i) and shop3["message_id"] is None and shop3["message_channel_id"] is None and shop["message_id"] in deleted,
       "unpublish: message deleted, columns cleared")
    ok(shop3["title"] == "Hello" and shop3["channel_id"] == 42, "unpublish: settings kept")
    i = FakeInteraction(admin, guild); await cmds["unpublish"](i, panel=str(pid))
    ok("isn't published" in said(i), "unpublish: already unpublished handled")
    qp5 = await db.get("panels", qp["id"])
    i = FakeInteraction(admin, guild); await cmds["unpublish"](i, panel=str(qp["id"]))
    qp6 = await db.get("panels", qp["id"])
    ok(qp5["message_id"] in deleted and all(m in deleted for m in json.loads(qp5["image_message_ids"]))
       and json.loads(qp6["image_message_ids"]) == [], "unpublish: quick panel removes panel + pictures")
    i = FakeInteraction(admin, guild); await cmds["panels"](i)
    ok("not posted" in said(i), "panels: shows unpublished state")
    i = FakeInteraction(admin, guild); await cmds["publish"](i, panel=str(qp["id"]))
    qp7 = await db.get("panels", qp["id"])
    ok("published" in said(i) and qp7["message_id"] and json.loads(qp7["image_message_ids"]), "publish: round trip after unpublish re-posts pictures + panel")
    ok((await db.fetchone("SELECT COUNT(*) AS n FROM dropdown_options o JOIN panel_dropdowns d ON d.id=o.dropdown_id WHERE d.panel_id=?", (qp["id"],)))["n"] >= 1,
       "publish/unpublish: dropdown + options untouched")
    # unreachable channel note
    await db.update("panels", pid, {"message_id": 5, "message_channel_id": 123456})
    i = FakeInteraction(admin, guild); await cmds["unpublish"](i, panel=str(pid))
    ok("couldn't reach" in said(i), "unpublish: warns when the old channel can't be reached")

    # ---- /ticket settings
    await db.update("guild_settings", 1, {"log_channel_id": 42, "archive_channel_id": 42, "max_open_tickets": 0})
    i = FakeInteraction(admin, guild); await cmds["settings"](i)
    se = embed_of(i); fv = {n: v for n, v, _ in se.fields}
    ok("<@&10>" in fv["Staff roles"] and fv["Admin roles"] == "—" and fv["Archive channel"] == "<#42>", "settings: roles + channels")
    ok(fv["Max open per member"] == "unlimited" and fv["Auto transcripts"] == "on", "settings: limits + toggles")
    ok(all(v.strip() and len(v) <= 1024 for v in fv.values()), "settings: every field non-empty and within limits")
    ok("panels" in fv["Totals"] and "open tickets" in fv["Totals"], "settings: totals")

    # ---- ticket fixtures for list / info / close / reopen / delete
    cat = (await db.where("categories", guild_id=1))[0]
    async def mk_ticket(number, owner, status, with_channel=True, claimed=None):
        cid = 9000 + number
        if with_channel:
            c = FakeChannel(cid, f"ticket-{number:04d}", guild); guild.channels[cid] = c
            async def empty(**k):
                return
                yield
            c.history = empty
        tid = await db.insert("tickets", {"guild_id": 1, "number": number, "channel_id": cid, "owner_id": owner,
                                          "category_id": cat["id"], "status": status, "claimed_by": claimed,
                                          "created_at": tickets.now_iso(),
                                          "closed_at": tickets.now_iso() if status != "open" else None,
                                          "closed_by": 3 if status != "open" else None})
        return await db.get("tickets", tid)
    t_open = await mk_ticket(10, 2, "open", claimed=3)
    t_open2 = await mk_ticket(11, 60, "open")
    t_closed = await mk_ticket(12, 2, "closed")
    t_ghost = await mk_ticket(13, 2, "open", with_channel=False)

    # ---- /ticket list
    i = FakeInteraction(admin, guild); await cmds["list"](i)
    txt = said(i)
    ok("#0010" in txt and "#0011" in txt and "#0013" in txt and "#0012" not in txt and "#0001" not in txt, "list: default = open only")
    ok(txt.index("#0013") < txt.index("#0011") < txt.index("#0010"), "list: newest first")
    ok("claimed by <@3>" in txt and "Order Support" in txt and "<#9010>" in txt, "list: claimer, category, channel shown")
    i = FakeInteraction(admin, guild); await cmds["list"](i, status="closed")
    ok("#0012" in said(i) and "#0010" not in said(i), "list: closed filter")
    i = FakeInteraction(admin, guild); await cmds["list"](i, status="deleted")
    ok("#0001" in said(i) and "<#" not in said(i).split("\n\n", 1)[-1].split("#0001")[1].split("\n")[0].split("·")[1], "list: deleted ticket shows no channel mention")
    i = FakeInteraction(admin, guild); await cmds["list"](i, status="all")
    ok(all(f"#{n:04d}" in said(i) for n in (1, 10, 11, 12, 13)), "list: all")
    i = FakeInteraction(admin, guild); await cmds["list"](i, status="all", user=NS(id=60))
    ok("#0011" in said(i) and "#0010" not in said(i) and "opened by <@60>" in said(i), "list: user filter")
    i = FakeInteraction(admin, guild); await cmds["list"](i, status="open", user=NS(id=999))
    ok("No tickets match" in said(i), "list: empty result handled")
    for n in range(100, 140):                                                  # more than the cap
        await db.insert("tickets", {"guild_id": 1, "number": n, "channel_id": 5000 + n, "owner_id": 2, "category_id": cat["id"],
                                    "status": "closed", "created_at": tickets.now_iso()})
    i = FakeInteraction(admin, guild); await cmds["list"](i, status="closed")
    e = embed_of(i)
    ok(len(e.description) < 4096 and "Showing 25 of 41" in e._footer[0], "list: capped at 25, total reported, within embed limit")

    # ---- /ticket info
    i = FakeInteraction(bob, guild); await cmds["info"](i, number=10)
    fv = {n: v for n, v, _ in embed_of(i).fields}
    ok(fv["Status"] == "open" and fv["Owner"] == "<@2>" and fv["Category"] == "Order Support" and fv["Claimed by"] == "<@3>"
       and fv["Channel"] == "<#9010>" and fv["Opened"].startswith("<t:"), "info: shows ticket details")
    i = FakeInteraction(bob, guild); await cmds["info"](i, number=12)
    fv = {n: v for n, v, _ in embed_of(i).fields}
    ok("closed" in fv["Status"] and "<@3>" in fv["Status"] and "Closed" in fv, "info: closed ticket shows who closed it")
    i = FakeInteraction(bob, guild); await cmds["info"](i, number=1)
    fv = {n: v for n, v, _ in embed_of(i).fields}
    ok(fv["Channel"] == "deleted", "info: deleted ticket")
    i = FakeInteraction(bob, guild, channel=guild.channels[9010]); await cmds["info"](i)
    ok("#0010" in embed_of(i).title, "info: defaults to the current ticket channel")
    i = FakeInteraction(bob, guild, channel=dest); await cmds["info"](i)
    ok("inside a ticket channel" in said(i), "info: outside a ticket without number -> hint")
    i = FakeInteraction(bob, guild); await cmds["info"](i, number=9999)
    ok("no ticket #9999" in said(i), "info: unknown number")
    i = FakeInteraction(eve, guild); await cmds["info"](i, number=10)
    ok("Staff only" in said(i), "info: non-staff denied")
    i = FakeInteraction(admin, guild); await cmds["info"](i, number=10)
    ok(i.response.log[0][2].get("ephemeral") is True, "info: admin allowed, ephemeral")

    # ---- /ticket close (confirmation on)
    await db.update("guild_settings", 1, {"close_confirmation": 1})
    ch10 = guild.channels[9010]
    i = FakeInteraction(eve, guild); await cmds["close"](i, number=10)
    ok("can't close" in said(i), "close: stranger denied")
    i = FakeInteraction(bob, guild); await cmds["close"](i, number=12)
    ok("already closed" in said(i), "close: already closed")
    i = FakeInteraction(bob, guild); await cmds["close"](i, number=13)
    ok("can't find that ticket's channel" in said(i), "close: missing channel handled")
    i = FakeInteraction(bob, guild); await cmds["close"](i, number=10)
    view = i.response.log[0][2]["view"]
    ok("Close ticket #0010" in said(i) and (await db.get("tickets", t_open["id"]))["status"] == "open", "close: asks for confirmation first")
    yes_i = FakeInteraction(bob, guild, channel=ch10, message=object()); await view.yes.callback(yes_i)
    ok((await db.get("tickets", t_open["id"]))["status"] == "closed" and (await db.get("tickets", t_open["id"]))["closed_by"] == 3,
       "close: confirmed -> ticket closed by the clicker")
    ok("closed" in str(yes_i.response.log[-1][2].get("content")), "close: confirmation message updated")
    ok(any("Ticket closed" in str(k.get("embed").title) for _, k in ch10.sent if k.get("embed")), "close: closed embed posted in the ticket")
    # owner may close own ticket (category allows it), no confirmation this time
    await db.update("guild_settings", 1, {"close_confirmation": 0})
    i = FakeInteraction(alice, guild, channel=guild.channels[9011]); await cmds["close"](i)
    ok("can't close" in said(i) and (await db.get("tickets", t_open2["id"]))["status"] == "open", "close: member who is not the owner and not staff is denied")
    i = FakeInteraction(eve, guild, channel=guild.channels[9011]); await cmds["close"](i)
    ok((await db.get("tickets", t_open2["id"]))["status"] == "closed" and "Ticket #0011 closed" in said(i), "close: owner can close their own ticket (no confirmation setting)")
    await db.update("categories", cat["id"], {"owner_can_close": 0})
    t14 = await mk_ticket(14, 60, "open")
    i = FakeInteraction(eve, guild, channel=guild.channels[9014]); await cmds["close"](i)
    ok("can't close" in said(i) and (await db.get("tickets", t14["id"]))["status"] == "open", "close: owner blocked when category forbids it")
    await db.update("categories", cat["id"], {"owner_can_close": 1})

    # ---- /ticket reopen
    i = FakeInteraction(eve, guild); await cmds["reopen"](i, number=10)
    ok("Only ticket staff" in said(i), "reopen: non-staff denied")
    i = FakeInteraction(bob, guild); await cmds["reopen"](i, number=14)
    ok("isn't closed" in said(i), "reopen: open ticket rejected")
    bot.user = NS(id=500)
    stale = NS(author=NS(id=500), embeds=[], components=[NS(children=[NS(custom_id="tb:t:reopen")])], edited=None)
    async def stale_edit(**k): stale.edited = k
    stale.edit = stale_edit
    chatter = NS(author=NS(id=2), embeds=[], components=[], edit=None)
    async def hist_with_stale(**k):
        for m in (chatter, stale): yield m
    ch10.history = hist_with_stale
    i = FakeInteraction(bob, guild); await cmds["reopen"](i, number=10)
    ok((await db.get("tickets", t_open["id"]))["status"] == "open" and "reopened" in said(i), "reopen: closed ticket reopened")
    ok(stale.edited == {"view": None}, "reopen: old 'Ticket closed' buttons removed (like the Reopen button)")
    ok(any("reopened" in str(k.get("embed").title).lower() for _, k in ch10.sent if k.get("embed")), "reopen: announcement posted in ticket")
    i = FakeInteraction(bob, guild); await cmds["reopen"](i, number=13)
    ok("isn't closed" in said(i) or "find that ticket's channel" in said(i), "reopen: missing-channel ticket handled")

    # ---- /ticket delete
    i = FakeInteraction(eve, guild); await cmds["delete"](i, number=10)
    ok("Only ticket staff" in said(i), "delete: non-staff denied")
    i = FakeInteraction(bob, guild); await cmds["delete"](i, number=1)
    ok("already deleted" in said(i), "delete: already deleted")
    i = FakeInteraction(bob, guild); await cmds["delete"](i, number=13)
    ok("find that ticket's channel" in said(i), "delete: missing channel handled")
    i = FakeInteraction(bob, guild); await cmds["delete"](i, number=10)
    view = i.response.log[0][2]["view"]
    ok("Delete ticket #0010" in said(i) and (await db.get("tickets", t_open["id"]))["status"] == "open", "delete: always asks first")
    yes_i = FakeInteraction(bob, guild, channel=ch10, message=object()); await view.yes.callback(yes_i)
    ok((await db.get("tickets", t_open["id"]))["status"] == "deleted" and getattr(ch10, "deleted", False), "delete: ticket + channel removed after confirming")
    ok(len(await db.where("transcripts", ticket_id=t_open["id"])) >= 1, "delete: transcript saved first")
    ok(any("deleted" in str(r[2].get("content")) for r in yes_i.response.log if r[0] == "edit_orig"), "delete: success message shown")

    # ---- the rest of the bot is unaffected: /ticket transcript still gated
    i = FakeInteraction(eve, guild, channel=guild.channels[9011]); await cmds["transcript"](i)
    ok("Staff only" in said(i), "transcript: still staff only")

    # =====================================================================
    # Unlimited pictures, GIFs, server emojis and stickers
    # =====================================================================
    from ticketbot import media, images as imgmod
    class Emo:
        def __init__(s, name, id, animated=False): s.name, s.id, s.animated, s.available = name, id, animated, True
        def __str__(s): return f"<{'a' if s.animated else ''}:{s.name}:{s.id}>"
    class Stk:
        def __init__(s, name, id): s.name, s.id = name, id
    guild.emojis = [Emo("rin_happy", 111111111111111111), Emo("dance", 222222222222222222, True)]
    guild.stickers = [Stk("Wave", 5), Stk("Cheer", 6)]

    # emoji shorthand
    ok(media.emojify(guild, "hi :rin_happy: and :DANCE:!") == "hi <:rin_happy:111111111111111111> and <a:dance:222222222222222222>!", "emoji: :name: -> server emoji (case-insensitive, animated)")
    ok(media.emojify(guild, "<:rin_happy:111111111111111111> 10:30:45 :nope:") == "<:rin_happy:111111111111111111> 10:30:45 :nope:", "emoji: full tags, times and unknown names untouched")
    ok(media.emojify(guild, ":rin_happy::dance:") == "<:rin_happy:111111111111111111><a:dance:222222222222222222>", "emoji: adjacent shortcodes")
    r = media.rich_text(guild, "Hello {sticker:wave}\n{sticker:Nope} :rin_happy:")
    ok([x.name for x in r.stickers] == ["Wave"] and r.missing == ["Nope"] and "{sticker" not in r.text and "<:rin_happy:" in r.text, "sticker: token extracted, unknown reported, emoji resolved")
    ok(len(media.rich_text(guild, "{sticker:Wave}{sticker:Cheer}").stickers) == 2, "sticker: several in one text")
    ok(media.split_leading_emoji(guild, ":rin_happy: Order") == ("<:rin_happy:111111111111111111>", "Order"), "label: leading :emoji: moves to emoji slot")
    ok(media.split_leading_emoji(guild, "Order") == (None, "Order"), "label: plain label untouched")
    ok(util.classify_link("https://tenor.com/view/cat-gif-123") == "link" and util.classify_link("https://media.tenor.com/x/cat.gif") == "image"
       and util.classify_link("https://giphy.com/gifs/abc") == "link" and util.classify_link("https://x/a.png") == "image", "gif: Tenor/Giphy pages are links, direct files are images")

    # modals accept :emoji:
    cat2 = None
    await submit(E["button"], "Button", None, panel["id"], {"label": ":rin_happy: Help", "emoji": "", "style": "primary", "action": "create"})
    b2 = (await db.where("panel_buttons", order="id", panel_id=panel["id"]))[-1] if await db.where("panel_buttons", panel_id=panel["id"]) else None
    # (the first test panel was deleted above; use a fresh one)
    await submit(E["panel"], "Basics", None, None, {"name": "Emo", "title": ":rin_happy: Title", "description": "d :dance: {sticker:Wave}", "color": ""})
    ep = (await db.where("panels", guild_id=1, name="Emo"))[0]
    await submit(E["button"], "Button", None, ep["id"], {"label": ":rin_happy: Help", "emoji": "", "style": "primary", "action": "create"})
    await submit(E["button"], "Button", None, ep["id"], {"label": "Two", "emoji": ":dance:", "style": "primary", "action": "create"})
    eb = await db.where("panel_buttons", order="id", panel_id=ep["id"])
    ok(eb[0]["emoji"] == "<:rin_happy:111111111111111111>" and eb[0]["label"] == "Help", "button: ':emoji: label' -> emoji slot + label")
    ok(eb[1]["emoji"] == "<a:dance:222222222222222222>", "button: emoji box accepts :name:")
    i = await submit(E["button"], "Button", None, ep["id"], {"label": "x", "emoji": ":typo:", "style": "primary", "action": "create"})
    ok("emoji" in i.response.log[0][1][0].lower(), "button: unknown :name: rejected")
    await db.update("panel_buttons", eb[0]["id"], {"category_id": cat["id"]}); await db.update("panel_buttons", eb[1]["id"], {"category_id": cat["id"]})
    await submit(E["embed"], "Main", None, None, {"name": "EmoEmbed", "title": ":rin_happy: Title", "description": "d :dance: {sticker:Wave}", "color": ""})
    eemb = (await db.where("embed_templates", guild_id=1, name="EmoEmbed"))[0]
    await db.update("panels", ep["id"], {"channel_id": 42, "content": "Hi :rin_happy: {sticker:Cheer}", "embed_ids": json.dumps([eemb["id"]])})
    n = len(dest.sent)
    msg = await panels.publish(bot, guild, ep["id"])
    k = dest.sent[n][1]
    ok("published" in msg and k["content"] == "Hi <:rin_happy:111111111111111111>" and [x.name for x in k["stickers"]] == ["Cheer", "Wave"],
       "panel: emojis resolved in text, stickers from text + description attached")
    ok(k["embeds"][0].title == "<:rin_happy:111111111111111111> Title" and "{sticker" not in (k["embeds"][0].description or ""), "panel: embed title/description rich, sticker token hidden")
    await db.update("panels", ep["id"], {"content": "{sticker:Ghost}"})
    msg = await panels.publish(bot, guild, ep["id"])
    ok("no sticker called **Ghost**" in msg, "panel: unknown sticker reported")

    # ticket welcome message: emoji + sticker
    await db.update("guild_settings", 1, {"max_open_tickets": 0, "prevent_duplicates": 0})
    await db.update("categories", cat["id"], {"form_id": None, "welcome_message": "Welcome {user} :rin_happy: {sticker:Wave}"})
    wi = FakeInteraction(alice, guild, channel=dest)
    await tickets.handle_entry(wi, bot, category_id=cat["id"], action="category", form_id=None, panel_id=None)
    wch = guild.channels[(await db.fetchone("SELECT channel_id FROM tickets ORDER BY id DESC LIMIT 1"))["channel_id"]]
    wk = wch.sent[0][1]
    ok("<:rin_happy:111111111111111111>" in wk["embed"].description and [x.name for x in wk["stickers"]] == ["Wave"], "welcome: emoji resolved + sticker sent")

    # ---- /ticket image
    ich = FakeChannel(88, "pics", guild); guild.channels[88] = ich
    pdeleted = []
    class _PM2:
        def __init__(s, mid): s.id = mid
        async def delete(s): pdeleted.append(s.id)
    ich.get_partial_message = lambda mid: _PM2(mid)
    ic = cmds["image"].cmds
    ipid = await db.insert("panels", {"guild_id": 1, "name": "Pics", "channel_id": 88, "color": 0x123456})
    for name, fn in ic.items():
        i = FakeInteraction(eve, guild, channel=ich)
        kw = {"number": 1} if name == "remove" else {}
        await fn(i, **kw)
        ok("Administrator" in said(i), f"/image {name}: denied for non-admin")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics")
    ok("either a `link` or a `file`" in said(i), "image add: needs link or file")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", link="nope")
    ok("http" in said(i), "image add: bad link rejected")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Nope", link="https://x/1.png")
    ok("couldn't find" in said(i), "image add: unknown panel")
    # one by one: 25 pictures -> not limited
    for n_ in range(1, 26):
        i = FakeInteraction(admin, guild, channel=ich)
        await ic["add"](i, panel="Pics", link=f"https://x/{n_}.png", repost=False)
    ok(len(await media.list_images(db, ipid)) == 25 and "isn't posted yet" in said(i), "image add: 25 pictures added one by one, no limit")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", link="https://x/first.png", position=1, repost=False)
    ok((await media.list_images(db, ipid))[0]["url"] == "https://x/first.png" and "#1" in said(i), "image add: position inserts at the front")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", link="https://tenor.com/view/cat-gif-9", repost=False)
    ok((await media.list_images(db, ipid))[-1]["kind"] == "link", "image add: Tenor page stored as link")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", link="https://cdn.discordapp.com/attachments/1/2/a.png?ex=1", repost=False)
    ok("stop working" in said(i), "image add: warns about expiring Discord links")
    # bulk (modal)
    i = FakeInteraction(admin, guild, channel=ich); await ic["bulk"](i, panel="Pics", repost=False)
    modal = i.response.log[0][1][0]; modal.links.value = "https://x/b1.gif\nhttps://x/b2.gif, https://x/b3.webp"
    bi = FakeInteraction(admin, guild, channel=ich); await modal.on_submit(bi)
    ok(len(await media.list_images(db, ipid)) == 31 and "Added **3**" in said(bi), "image bulk: 3 links added in order")
    modal.links.value = "https://x/ok.png\nbroken"
    bi = FakeInteraction(admin, guild, channel=ich); await modal.on_submit(bi)
    ok(len(await media.list_images(db, ipid)) == 31 and "Nothing was added" in said(bi), "image bulk: one bad line rejects the batch")
    # list
    i = FakeInteraction(admin, guild, channel=ich); await ic["list"](i, panel="Pics")
    ok("`1.`" in said(i) and "GIF link" in said(i), "image list: numbered, shows kinds")
    # upload (file)
    import tempfile as _tf
    os.environ["IMAGE_DIR"] = _tf.mkdtemp()
    class Att:
        content_type = "image/gif"; filename = "party.gif"; size = 1234
        async def save(s, path): open(path, "wb").write(b"GIF89a")
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", file=Att(), repost=False)
    up = (await media.list_images(db, ipid))[-1]
    ok(up["kind"] == "file" and os.path.isfile(up["file_path"]) and up["label"] == "party.gif", "image add: uploaded file saved on disk")
    big = Att(); big.size = 10**9
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", file=big, repost=False)
    ok("upload limit" in said(i), "image add: oversized file rejected")
    txt = Att(); txt.content_type = "text/plain"; txt.filename = "a.txt"
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", file=txt, repost=False)
    ok("isn't a picture" in said(i), "image add: non-picture rejected")
    # publish: 33 pictures + upload -> chunks of 10, links alone, order kept, panel last
    n = len(ich.sent)
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", link="https://x/last.png")   # not posted yet -> hint only
    ok("isn't posted yet" in said(i), "image add: unposted panel gets a publish hint")
    msg = await panels.publish(bot, guild, ipid)
    posted = ich.sent[n:]
    embed_counts = [len(k["embeds"]) for _, k in posted[:-1] if "embeds" in k]
    ok("published" in msg and all(c <= 10 for c in embed_counts) and sum(embed_counts) == 32, f"publish: 32 pictures split into messages of <=10 embeds {embed_counts}")
    ok(any(k.get("content") == "https://tenor.com/view/cat-gif-9" for _, k in posted), "publish: Tenor link posted as plain text")
    ok(any(k.get("files") for _, k in posted if "embeds" in k), "publish: uploaded file attached")
    upmsg = [k for _, k in posted if k.get("files")][0]
    ok(any(e._image and e._image.startswith("attachment://image-") for e in upmsg["embeds"]), "publish: upload shown via attachment://")
    ok(panels._message_kwargs([], None)["content"] == media.ZWSP, "publish: a dropdown-only panel message gets an invisible carrier text")
    first_ids = json.loads((await db.get("panels", ipid))["image_message_ids"])
    ok(len(first_ids) == len(posted) - 1, "publish: gallery message ids stored")
    # re-post replaces old gallery
    pdeleted.clear()
    i = FakeInteraction(admin, guild, channel=ich); await ic["add"](i, panel="Pics", link="https://x/again.png")
    ok(set(first_ids) <= set(pdeleted), "image add on a posted panel: re-posts and deletes the old pictures")
    # remove range / purge
    total = len(await media.list_images(db, ipid))
    i = FakeInteraction(admin, guild, channel=ich); await ic["remove"](i, number=0, panel="Pics")
    ok("between 1 and" in said(i), "image remove: bad number")
    i = FakeInteraction(admin, guild, channel=ich); await ic["remove"](i, number=1, last=3, panel="Pics", repost=False)
    ok(len(await media.list_images(db, ipid)) == total - 3 and [r["position"] for r in await media.list_images(db, ipid)] == list(range(total - 3)), "image remove: range removed, positions renumbered")
    upfile = up["file_path"]
    rows = await media.list_images(db, ipid)
    idx = [r["id"] for r in rows].index(up["id"]) + 1
    i = FakeInteraction(admin, guild, channel=ich); await ic["remove"](i, number=idx, panel="Pics", repost=False)
    ok(not os.path.exists(upfile), "image remove: orphaned upload deleted from disk")
    # clear asks first
    i = FakeInteraction(admin, guild, channel=ich); await ic["clear"](i, panel="Pics")
    cv = i.response.log[0][2]["view"]; ok(len(await media.list_images(db, ipid)) > 0, "image clear: asks first")
    yi = FakeInteraction(admin, guild, channel=ich, message=object()); await cv.yes.callback(yi)
    ok(await media.list_images(db, ipid) == [], "image clear: everything removed after confirming")
    # default panel = this channel's quick panel
    i = FakeInteraction(admin, guild, channel=qch); await ic["add"](i, link="https://x/q1.png", repost=False)
    ok(len(await media.list_images(db, qp["id"])) >= 1, "image add: panel defaults to the channel's quick panel")
    i = FakeInteraction(admin, guild, channel=FakeChannel(555, "x", guild)); await ic["add"](i, link="https://x/q1.png")
    ok("Pick a `panel`" in said(i), "image add: asks for a panel when it can't tell")
    # quickpanel keeps pictures on re-run, replaces them when new links are given
    qimgs = len(await media.list_images(db, qp["id"]))
    await run_quick({"f_text": "again", "f_bar": "", "f_icon": "", "f_placeholder": "p", "f_topics": "Order | Buy | :rin_happy:"})
    qpn = await db.fetchone("SELECT * FROM panels WHERE name = ?", ("quick-77",))
    ok(len(await media.list_images(db, qpn["id"])) == qimgs, "quickpanel re-run keeps all pictures")
    opt = (await db.fetchall("SELECT o.* FROM dropdown_options o JOIN panel_dropdowns d ON d.id=o.dropdown_id WHERE d.panel_id=?", (qpn["id"],)))[0]
    ok(opt["emoji"] == "<:rin_happy:111111111111111111>", "quickpanel topics: :emoji: symbol resolved")
    ok(quick.parse_topics(":rin_happy: Order | d", guild)[0][0]["emoji"] == "<:rin_happy:111111111111111111>" and quick.parse_topics(":rin_happy: Order", guild)[0][0]["label"] == "Order", "quickpanel topics: leading :emoji: moved out of label")

    # ---- /ticket post
    ok("post" in cmds, "post registered")
    i = FakeInteraction(eve, guild, channel=ich); await cmds["post"](i)
    ok("Administrator" in said(i), "post: denied for non-admin")
    class Att2:
        async def to_file(s): return discord.File("x.png")
    i = FakeInteraction(admin, guild, channel=ich); await cmds["post"](i, channel=qch, file=Att2())
    sm = i.response.log[0][1][0]; sm.text.value = "Hello :rin_happy: {sticker:Wave}"
    n = len(qch.sent); si = FakeInteraction(admin, guild, channel=ich); await sm.on_submit(si)
    sk = qch.sent[n][1]
    ok(sk["content"] == "Hello <:rin_happy:111111111111111111>" and "embeds" not in sk and "embed" not in sk and [x.name for x in sk["stickers"]] == ["Wave"] and len(sk["files"]) == 1,
       "post: plain message with emoji, sticker and file, no embed")
    sm2 = (lambda m: m)(PostModal) if False else None
    from ticketbot.post import PostModal as SM
    m3 = SM(bot, qch, []); m3.text.value = "  "; si = FakeInteraction(admin, guild, channel=ich); await m3.on_submit(si)
    ok("empty" in said(si), "post: empty message rejected")

    # ---- /ticket imageurl
    from ticketbot import post as postmod
    class Att3:
        content_type = "image/png"; filename = "a.png"; size = 100
        async def read(s): return b"png"
    async def fake_up(data, name): return "https://files.catbox.moe/abc.png"
    postmod.upload_to_host = fake_up
    i = FakeInteraction(eve, guild); await cmds["imageurl"](i, file=Att3())
    ok("Administrator" in said(i), "imageurl: denied for non-admin")
    i = FakeInteraction(admin, guild); await cmds["imageurl"](i, file=Att3())
    ok("https://files.catbox.moe/abc.png" in said(i), "imageurl: returns the link")
    async def bad_up(data, name): raise RuntimeError("down")
    postmod.upload_to_host = bad_up
    i = FakeInteraction(admin, guild); await cmds["imageurl"](i, file=Att3())
    ok("Upload failed: down" in said(i), "imageurl: failure explained")
    big = Att3(); big.size = 10**9; i = FakeInteraction(admin, guild); await cmds["imageurl"](i, file=big)
    ok("too big" in said(i), "imageurl: oversized rejected")

    # legacy quick panel (banner stored in an embed template) is migrated
    ldb = Database(os.path.join(tempfile.mkdtemp(), "old.db")); await ldb.connect()
    lt = await ldb.insert("embed_templates", {"guild_id": 1, "name": "quick-5-banner", "image_url": "https://x/old.png", "thumbnail_url": "https://x/th.png"})
    await ldb.insert("panels", {"guild_id": 1, "name": "quick-5", "embed_ids": json.dumps([lt])})
    await ldb._migrate()
    lp = await ldb.fetchone("SELECT * FROM panels WHERE name = 'quick-5'")
    lim = await media.list_images(ldb, lp["id"])
    ok([r["url"] for r in lim] == ["https://x/old.png"] and lp["thumbnail_url"] == "https://x/th.png" and json.loads(lp["embed_ids"]) == []
       and await ldb.fetchone("SELECT id FROM embed_templates WHERE id = ?", (lt,)) is None, "migration: legacy quick banner becomes the first picture")
    await ldb._migrate(); ok(len(await media.list_images(ldb, lp["id"])) == 1, "migration: idempotent")
    # failure while posting pictures leaves nothing behind
    real = ich.send; calls = []
    async def flaky(*a, **k):
        if len(calls) >= 1: raise discord.HTTPException()
        calls.append(1); return await real(*a, **k)
    for n_ in range(12): await db.insert("panel_images", {"panel_id": ipid, "url": f"https://x/f{n_}.png", "kind": "image", "position": n_})
    pdeleted.clear(); ich.send = flaky
    msg = await panels.publish(bot, guild, ipid); ich.send = real
    ok("couldn't post" in msg and len(pdeleted) == 1, "publish: failure midway deletes the partial gallery")

    # ---- order confirmation (before / after), queue, editable texts, server emojis, transcript look, box panel
    from ticketbot import orderflow, texts, transcripts
    from ticketbot.util import get_settings
    og = FakeChannel(9101, "order-0099", guild); guild.channels[9101] = og
    qch = FakeChannel(9102, "queue", guild); guild.channels[9102] = qch
    buyer = FakeMember(77, guild); guild.members[77] = buyer
    ocat = await db.insert("categories", {"guild_id": 1, "name": "OrderCat", "is_order": 1})
    answers = [{"q": "What?", "a": "A logo"}, {"q": "Details", "a": "blue"}]
    otid = await db.insert("tickets", {"guild_id": 1, "number": 99, "channel_id": 9101, "owner_id": 77,
                                       "category_id": ocat, "status": "open", "created_at": tickets.now_iso(),
                                       "form_answers": __import__("json").dumps(answers)})
    ticket = await db.get("tickets", otid); category = await db.get("categories", ocat)
    await db.update("guild_settings", 1, {"order_before_title": ":rin_happy: Check it", "queue_channel_id": 9102,
                                          "order_yes_label": "Looks good", "stage_queued": "Waiting :dance:"})
    settings = await get_settings(bot, 1)
    await orderflow.start(bot, og, ticket, category, answers, 5555, settings)
    before = og.sent[-1][1]
    ok(before["embed"].title.startswith("<:rin_happy:") and len(before["embed"].fields) == 2,
       "order before: confirmation shows the order, server emoji in the title")
    ok(before["view"].yes.label == "Looks good", "order before: button label comes from the settings")
    plain_cat = await db.insert("categories", {"guild_id": 1, "name": "NoOrder"})
    n_before = len(og.sent)
    await orderflow.start(bot, og, ticket, await db.get("categories", plain_cat), answers, None, settings)
    ok(len(og.sent) == n_before, "order flow: categories without Order flow are untouched")
    order = await db.fetchone("SELECT * FROM ticket_orders WHERE ticket_id = ?", (otid,))
    stranger = FakeMember(55, guild)
    v = orderflow.OrderConfirmView(bot)
    i = FakeInteraction(stranger, guild, channel=og); await v.yes.callback(i) if hasattr(v.yes, "callback") else None
    ok(await db.get("ticket_orders", order["id"]) and (await db.get("ticket_orders", order["id"]))["status"] == "pending",
       "order before: a stranger cannot confirm")
    i = FakeInteraction(buyer, guild, channel=og)
    await orderflow.confirm(bot, guild, og, order, ticket)
    order = await db.get("ticket_orders", order["id"])
    ok(order["status"] == "queued" and order["queue_message_id"], "order after: confirmed and queued")
    ok(qch.sent and qch.sent[-1][1]["embed"].title.startswith("♡ Order 0099"), "queue: message posted in the queue channel")
    ok("Waiting <a:dance:" in qch.sent[-1][1]["embed"].description, "queue: stage name editable + server emoji")
    ok(any("Order confirmed" in str(k["embed"].title) and "number **1**" in k["embed"].description
           for _, k in og.sent if k.get("embed")), "order after: confirmation with queue position")
    await db.update("guild_settings", 1, {"queue_update_text": "Hey! {code} is {status} :rin_happy:"})
    await orderflow.set_status(bot, guild, order, "processing", admin)
    ok((await db.get("ticket_orders", order["id"]))["status"] == "processing"
       and "<:rin_happy:" in og.sent[-1][1]["embed"].description, "queue: status update in the ticket uses the edited text")
    ok(buyer.dms and "Hey!" in buyer.dms[-1][1]["embed"].description, "queue: buyer notified by DM")
    await orderflow.set_status(bot, guild, await db.get("ticket_orders", order["id"]), "done", admin)
    ok((await db.get("ticket_orders", order["id"]))["status"] == "done", "queue: order marked done")
    bc = FakeChannel(9103, "board", guild); guild.channels[9103] = bc
    ok("board posted" in (await orderflow.post_board(bot, guild, bc)).lower() and bc.sent, "queue: live board posted")

    # editable ticket messages
    await db.update("guild_settings", 1, {"msg_close_title": ":rin_happy: Bye", "form_title": "Order form"})
    settings = await get_settings(bot, 1)
    ok(texts.say(guild, settings, "msg_close_title").startswith("<:rin_happy:"), "messages: server emoji works in edited texts")
    ok(tickets.form_embed(guild, settings, answers).title == "Order form", "messages: form title editable")
    ok(texts.say(guild, {}, "stage_done") == "Done", "messages: blank setting falls back to the default")

    # transcript look
    await db.update("guild_settings", 1, {"tr_title": "Receipt {number}", "tr_footer": "bye {server}", "tr_theme": "light",
                                          "tr_color": 0xFFB7D5, "tr_show_log": 0})
    html_doc = await transcripts.render_html(bot, og, ticket, [], [], category, "buyer", "—", await get_settings(bot, 1))
    ok("Receipt 0099" in html_doc and "bye Guild" in html_doc and "ffb7d5" in html_doc and "Ticket activity" not in html_doc
       and "#f2f3f5" in html_doc, "transcript: title, footer, color, theme and sections editable")

    # box panel
    bpid = await db.insert("panels", {"guild_id": 1, "name": "boxy", "content": "Hi :rin_happy:", "embed_ids": "[]",
                                      "channel_id": 42, "layout": "box"})
    bdd = await db.insert("panel_dropdowns", {"panel_id": bpid, "placeholder": "Pick", "min_values": 1, "max_values": 1})
    await db.insert("dropdown_options", {"dropdown_id": bdd, "label": "Buy", "category_id": ocat, "position": 0})
    msg = await panels.publish(bot, guild, bpid)
    sent = dest.sent[-1][1]
    ok("Box panel published" in msg and "view" in sent and "embeds" not in sent and "content" not in sent,
       "box panel: published as one box (no embeds / plain content)")
    ok(panels.is_box(await db.get("panels", bpid)), "box panel: layout stored")

asyncio.run(main())
print("\nRESULT:", "FAILURES" if ok.failed else "ALL PASSED")
sys.exit(1 if ok.failed else 0)
