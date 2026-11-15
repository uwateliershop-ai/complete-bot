"""Offline test for the shop layer (orders.py): run with  python tests/orders_test.py
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
from ticketbot import orders


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
    # ---- pure helpers
    ok(orders.parse_price("₱1,250.50") == 1250.5 and orders.parse_price("abc") is None, "parse_price")
    ok(orders.normalize_method("GCash pls") == "gcash" and orders.normalize_method("cash") is None, "normalize_method")
    ok("**lined up**" in orders.progress_text("lined_up") and "~~donesies~~" in orders.progress_text("lined_up"), "progress: future stages struck")
    ok("**donesies**" in orders.progress_text("done") and "~~" not in orders.progress_text("done"), "progress: done")
    r = {"trigger": "price, how much", "match_type": "contains"}
    ok(orders.reply_matches(r, "How much is it?") and not orders.reply_matches(r, "priceless art"), "keyword match uses whole words")
    ok(orders.reply_matches({"trigger": "hi", "match_type": "exact"}, "Hi") and not orders.reply_matches({"trigger": "hi", "match_type": "exact"}, "hi there"), "exact match")

    # ---- environment
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db")); await db.connect()
    guild = FakeGuild()
    staff = FakeMember(2, guild, admin=True); buyer = FakeMember(3, guild)
    guild.members.update({2: staff, 3: buyer})
    ticket_ch = FakeChannel(100, "order-0001", guild); queue_ch = FakeChannel(101, "queue", guild)
    vouch_ch = FakeChannel(102, "vouches", guild); deals_ch = FakeChannel(103, "deals", guild)
    for c in (ticket_ch, queue_ch, vouch_ch, deals_ch): guild.channels[c.id] = c
    bot = NS(db=db, get_guild=lambda i: guild)
    cat_id = await db.insert("categories", {"guild_id": 1, "name": "Order", "staff_role_ids": "[]"})
    other_id = await db.insert("categories", {"guild_id": 1, "name": "Report", "staff_role_ids": "[]"})
    category = await db.get("categories", cat_id)
    await orders.shop_update(bot, 1, queue_channel_id=101, vouch_channel_id=102, shop_name="awngel", gcash_info="0917 000 0000")
    tid = await db.insert("tickets", {"guild_id": 1, "number": 1, "channel_id": 100, "owner_id": 3, "category_id": cat_id,
                                      "status": "open", "created_at": orders.now_iso()})
    ticket = await db.get("tickets", tid)

    # ---- non-order category does not start the flow
    await orders.on_ticket_created(bot, ticket_ch, ticket, await db.get("categories", other_id), buyer, [])
    ok(await orders.order_by_channel(bot, 100) is None, "report ticket: no order flow")

    # ---- pledge
    await orders.on_ticket_created(bot, ticket_ch, ticket, category, buyer, [])
    order = await orders.order_by_channel(bot, 100)
    ok(order and order["status"] == "pledge" and order["code"] == "UW-0001", "order created in pledge stage")
    ok("awngel's pledge" in ticket_ch.sent[-1]["embed"].description and "i swear" in ticket_ch.sent[-1]["embed"].description, "pledge embed names shop + phrase")
    ok(ticket_ch.msgs[order["pledge_message_id"]].pinned, "pledge message pinned")

    await orders.handle_message(bot, NS(guild=guild, author=buyer, channel=ticket_ch, content="hello?"))
    ok((await orders.order_by_channel(bot, 100))["status"] == "pledge", "other text doesn't pass the pledge")
    await orders.handle_message(bot, NS(guild=guild, author=staff, channel=ticket_ch, content="i swear"))
    ok((await orders.order_by_channel(bot, 100))["status"] == "pledge", "staff typing the phrase doesn't count")
    await orders.handle_message(bot, NS(guild=guild, author=buyer, channel=ticket_ch, content=" I Swear "))
    ok((await orders.order_by_channel(bot, 100))["status"] == "form", "'i swear' (any case) unlocks the form")
    form_view = ticket_ch.sent[-1]["view"]

    # ---- order form
    i = FakeInteraction(staff, guild, ticket_ch)
    await press(form_view, "fill order form").callback(i)
    ok("owner only" in i.response.log[0][1][0], "form button: owner only")
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(form_view, "fill order form").callback(i)
    modal = i.response.log[0][1][0]
    ok(i.response.log[0][0] == "modal", "form button opens modal")
    modal.username.value, modal.item.value, modal.quantity.value, modal.method.value = "@angelfuwa", "bot package", "2", "gcash"
    modal.notes.value = ""
    i = FakeInteraction(buyer, guild, ticket_ch); modal.method.value = "cash"
    await modal.on_submit(i)
    ok("gcash" in i.response.log[0][1][0], "invalid payment mode is rejected")
    modal.method.value = "GCash"
    i = FakeInteraction(buyer, guild, ticket_ch); await modal.on_submit(i)
    order = await orders.order_by_channel(bot, 100)
    ok(order["status"] == "confirming" and order["quantity"] == 2 and order["pay_method"] == "gcash", "form saved, now confirming")
    confirm_view = ticket_ch.sent[-1]["view"]

    # ---- confirmation
    i = FakeInteraction(buyer, guild, ticket_ch, message=NS(id=1))
    await press(confirm_view, "yes, proceed").callback(i)
    order = await orders.order_by_channel(bot, 100)
    ok(order["status"] == "payment", "yes, proceed -> payment")
    ok(i.response.log[0][0] == "edit" and i.response.log[0][2]["view"].children[0].disabled, "confirm buttons replaced by disabled 'no touchie!'")
    pay_view = ticket_ch.sent[-1]["view"]
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(confirm_view, "yes, proceed").callback(i)
    ok("already confirmed" in i.response.log[0][1][0], "double confirm blocked")

    # ---- payment
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(pay_view, "gcash").callback(i)
    ok("0917" in i.response.log[0][2]["embed"].description, "gcash button shows saved instructions")
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(pay_view, "robux").callback(i)
    ok("handler will send" in i.response.log[0][2]["embed"].description and "commis" in i.response.log[0][2]["embed"].description,
       "robux: fallback text + commis note")
    ok((await orders.order_by_channel(bot, 100))["pay_method"] == "robux", "payment method follows the last button pressed")

    i = FakeInteraction(staff, guild, ticket_ch)
    await press(pay_view, "payment sent").callback(i)
    ok("owner only" in i.response.log[0][1][0], "payment sent: owner only")
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(pay_view, "payment sent").callback(i)
    sent_modal = i.response.log[0][1][0]; sent_modal.ref.value = "REF123"
    i = FakeInteraction(buyer, guild, ticket_ch); await sent_modal.on_submit(i)
    order = await orders.order_by_channel(bot, 100)
    ok(order["status"] == "paid_review" and order["payment_ref"] == "REF123", "payment sent -> staff review")
    proof_view = ticket_ch.sent[-1]["view"]

    # buyer can't confirm their own payment
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(proof_view, "confirm payment").callback(i)
    ok("only ticket staff" in i.response.log[0][1][0], "buyer can't confirm payment")

    # reject -> back to payment
    i = FakeInteraction(staff, guild, ticket_ch, message=NS(id=1))
    await press(proof_view, "reject").callback(i)
    ok((await orders.order_by_channel(bot, 100))["status"] == "payment", "reject returns order to payment stage")
    i = FakeInteraction(buyer, guild, ticket_ch)
    await press(pay_view, "payment sent").callback(i)
    m2 = i.response.log[0][1][0]; m2.ref.value = "REF456"
    await m2.on_submit(FakeInteraction(buyer, guild, ticket_ch))
    proof_view = ticket_ch.sent[-1]["view"]

    # ---- deal with 1 slot
    await db.insert("deals", {"guild_id": 1, "name": "Tender Love", "price": "₱39", "inclusions": "3 bot cmds", "slots_max": 1})
    deals = await db.where("deals", guild_id=1)
    await orders.shop_update(bot, 1, deals_channel_id=103, deals_message_id=deals_ch.msgs.setdefault(77, FakeMsg(id=77)).id)
    i = FakeInteraction(staff, guild, ticket_ch)
    await press(proof_view, "confirm payment").callback(i)
    pm = i.response.log[0][1][0]
    pm.price.value, pm.deal.value = "abc", ""
    i = FakeInteraction(staff, guild, ticket_ch); await pm.on_submit(i)
    ok("valid amount" in i.response.log[0][1][0], "invalid price rejected")
    pm.price.value, pm.deal.value = "39", "nope"
    i = FakeInteraction(staff, guild, ticket_ch); await pm.on_submit(i)
    ok("no deal named" in i.response.log[0][1][0], "unknown deal rejected")
    pm.price.value, pm.deal.value = "39", "tender love"
    i = FakeInteraction(staff, guild, ticket_ch); await pm.on_submit(i)
    order = await orders.order_by_channel(bot, 100)
    ok(order["status"] == "lined_up" and order["price"] == 39 and order["slot_held"] == 1, "confirm payment -> lined up, slot held")
    ok((await db.get("deals", deals[0]["id"]))["slots_used"] == 1, "deal slot count 1/1")
    ok(any(e for e in deals_ch.msgs[77].edits), "deals message refreshed live")
    ok(buyer.dms and "lined up" in buyer.dms[-1]["embed"].description, "buyer DMed when lined up")
    ok(queue_ch.sent and "status" in queue_ch.sent[-1]["embed"].description and "noted" in queue_ch.sent[-1]["embed"].description, "queue channel message posted")
    queue_view = queue_ch.sent[-1]["view"]

    # second buyer cannot take a full deal
    t2 = await db.insert("tickets", {"guild_id": 1, "number": 2, "channel_id": 200, "owner_id": 4, "category_id": cat_id,
                                     "status": "open", "created_at": orders.now_iso()})
    ch2 = FakeChannel(200, "order-0002", guild); guild.channels[200] = ch2
    await orders.on_ticket_created(bot, ch2, await db.get("tickets", t2), category, FakeMember(4, guild), [])
    o2 = await orders.order_by_channel(bot, 200)
    ok(o2["code"] == "UW-0002", "order numbers increment")
    await orders.update_order(bot, o2["id"], status="paid_review", item="x", pay_method="gcash")
    full = orders.PriceModal(bot, await db.get("orders", o2["id"]), deals, finalize=True)
    full.price.value, full.deal.value = "39", "Tender Love"
    i = FakeInteraction(staff, guild, ch2); await full.on_submit(i)
    ok("is full" in i.response.log[0][1][0] and (await db.get("orders", o2["id"]))["status"] == "paid_review", "full deal blocks a second order")

    # ---- queue buttons
    i = FakeInteraction(buyer, guild, queue_ch, message=NS(id=order["queue_message_id"]))
    await press(queue_view, "processing").callback(i)
    ok("only ticket staff" in i.response.log[0][1][0], "buyer can't press queue buttons")
    i = FakeInteraction(staff, guild, queue_ch, message=NS(id=order["queue_message_id"]))
    await press(queue_view, "processing").callback(i)
    ok((await orders.order_by_channel(bot, 100))["status"] == "processing", "processing button works")
    ok("processing" in queue_ch.msgs[order["queue_message_id"]].edits[-1]["embed"].description, "queue message edited")
    ok("**processing**" in ticket_ch.msgs[order["ticket_queue_message_id"]].edits[-1]["embed"].description, "ticket progress bar advanced")
    i = FakeInteraction(staff, guild, queue_ch, message=NS(id=order["queue_message_id"]))
    await press(queue_view, "processing").callback(i)
    ok("currently" in i.response.log[0][1][0], "can't go back to processing twice")
    i = FakeInteraction(staff, guild, queue_ch, message=NS(id=order["queue_message_id"]))
    await press(queue_view, "donesies").callback(i)
    order = await orders.order_by_channel(bot, 100)
    ok(order["status"] == "done" and order["slot_held"] == 0, "donesies: done + slot freed")
    ok((await db.get("deals", deals[0]["id"]))["slots_used"] == 0, "deal slot back to 0/1")
    ok(queue_ch.msgs[order["queue_message_id"]].edits[-1]["view"] is None, "queue buttons removed when done")
    vouch_view = ticket_ch.sent[-1]["view"]

    # ---- vouch
    i = FakeInteraction(buyer, guild, ticket_ch, message=NS(id=1))
    await press(vouch_view, "leave a vouch").callback(i)
    vm = i.response.log[0][1][0]
    vm.rating.value, vm.comment.value = "9", "great"
    i = FakeInteraction(buyer, guild, ticket_ch, message=NS(id=1)); await vm.on_submit(i)
    ok("1 to 5" in i.response.log[0][1][0], "bad rating rejected")
    vm.rating.value = "5"
    i = FakeInteraction(buyer, guild, ticket_ch, message=NS(id=1)); await vm.on_submit(i)
    ok(vouch_ch.sent and "★★★★★" in vouch_ch.sent[-1]["embed"].description, "vouch posted in vouch channel")
    i = FakeInteraction(buyer, guild, ticket_ch, message=NS(id=1)); await vm.on_submit(i)
    ok("already vouched" in i.response.log[0][1][0], "only one vouch per order")

    # ---- auto-responder
    await db.insert("autoreplies", {"guild_id": 1, "trigger": "price, magkano", "response": "hi {user}! see {shop} deals"})
    t3 = await db.insert("tickets", {"guild_id": 1, "number": 3, "channel_id": 300, "owner_id": 5, "category_id": other_id,
                                     "status": "open", "created_at": orders.now_iso()})
    ch3 = FakeChannel(300, "report-0003", guild); guild.channels[300] = ch3
    u5 = FakeMember(5, guild); guild.members[5] = u5
    msg = lambda text, who=u5: NS(guild=guild, author=who, channel=ch3, content=text)
    await orders.handle_message(bot, msg("magkano po?"))
    ok(len(ch3.sent) == 1 and "<@5>" in ch3.sent[0].get("content", ch3.sent[0].get("args", "")) or len(ch3.sent) == 1, "auto-reply fires on keyword")
    await orders.handle_message(bot, msg("price again?"))
    ok(len(ch3.sent) == 1, "same auto-reply on cooldown")
    await orders.handle_message(bot, msg("unrelated"))
    ok(len(ch3.sent) == 1, "no reply without a keyword")
    await orders.shop_update(bot, 1, autoreply_enabled=0); orders._reply_seen.clear()
    await orders.handle_message(bot, msg("price"))
    ok(len(ch3.sent) == 1, "auto-replies can be switched off")
    await orders.shop_update(bot, 1, autoreply_enabled=1); orders._reply_seen.clear()
    await db.update("tickets", t3, {"claimed_by": 2})
    await orders.handle_message(bot, msg("price"))
    ok(len(ch3.sent) == 1, "no auto-reply once a handler claimed the ticket")
    await orders.handle_message(bot, msg("hello", who=staff))
    ok((await db.get("tickets", t3))["staff_replied"] == 1, "staff message recorded")

    # ---- watcher: unclaimed nudge + idle reminder + auto-close
    old = (discord.utils.utcnow() - timedelta(minutes=30)).isoformat()
    t4 = await db.insert("tickets", {"guild_id": 1, "number": 4, "channel_id": 400, "owner_id": 6, "category_id": other_id,
                                     "status": "open", "created_at": old})
    ch4 = FakeChannel(400, "report-0004", guild); guild.channels[400] = ch4
    await orders.watch_once(bot)
    ok(len(ch4.sent) == 1 and "handler will be with u" in ch4.sent[0]["embed"].description, "unclaimed ticket gets one nudge")
    await orders.watch_once(bot)
    ok(len(ch4.sent) == 1, "nudge is sent only once")
    await orders.shop_update(bot, 1, idle_hours=1, autoclose_hours=1)
    await db.update("tickets", t4, {"last_activity": (discord.utils.utcnow() - timedelta(hours=2)).isoformat(), "staff_replied": 1})
    await orders.watch_once(bot)
    ok(len(ch4.sent) == 2 and "still there" in ch4.sent[1]["embed"].description, "idle buyer gets a reminder")
    ok(all(t["status"] != "closed" for t in await db.where("tickets", id=t4)), "not closed yet")

    print("\nRESULT:", "FAILED" if ok.failed else "ALL PASSED")
    return 1 if ok.failed else 0

sys.exit(asyncio.run(main()))
