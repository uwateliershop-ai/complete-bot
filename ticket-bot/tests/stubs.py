"""Minimal stand-ins for discord.py / aiosqlite so the bot logic can be exercised offline."""
import sys, types, sqlite3, enum, datetime, asyncio

# ---------------- aiosqlite shim ----------------
aio = types.ModuleType("aiosqlite")
aio.Row = sqlite3.Row
class _Cur:
    def __init__(s, cur): s.cur = cur; s.lastrowid = cur.lastrowid
    async def fetchall(s): return s.cur.fetchall()
class _Exec:
    def __init__(s, conn, sql, params): s.cur = conn.c.execute(sql, params)
    def __await__(s):
        async def f(): return _Cur(s.cur)
        return f().__await__()
    async def __aenter__(s): return _Cur(s.cur)
    async def __aexit__(s, *a): return False
class _Conn:
    def __init__(s, path): s.c = sqlite3.connect(path); s.c.row_factory = sqlite3.Row
    @property
    def row_factory(s): return s.c.row_factory
    @row_factory.setter
    def row_factory(s, v): s.c.row_factory = v
    def execute(s, sql, params=()): return _Exec(s, sql, params)
    async def executescript(s, sql): s.c.executescript(sql)
    async def commit(s): s.c.commit()
    async def close(s): s.c.close()
async def connect(path): return _Conn(path)
aio.connect = connect
sys.modules["aiosqlite"] = aio

# ---------------- discord shim ----------------
d = types.ModuleType("discord")
class HTTPException(Exception):
    text = "http error"
class NotFound(HTTPException): pass
class Forbidden(HTTPException): pass
d.HTTPException, d.NotFound, d.Forbidden = HTTPException, NotFound, Forbidden

class ButtonStyle(enum.Enum): primary=1; secondary=2; success=3; danger=4; link=5
class TextStyle(enum.Enum): short=1; paragraph=2
class ChannelType(enum.Enum): text=0; news=5; category=4
class MessageType(enum.Enum): default=0; reply=19; pins_add=6
d.ButtonStyle, d.TextStyle, d.ChannelType, d.MessageType = ButtonStyle, TextStyle, ChannelType, MessageType
class InteractionType(enum.Enum): component=3
d.InteractionType = InteractionType

class Object:
    def __init__(s, id, type=None): s.id = id; s.type = type
d.Object = Object
class Role:
    def __init__(s, id=0, name="r"): s.id, s.name, s.mention = id, name, f"<@&{id}>"
class Member:
    pass
class User: pass
class Guild: pass
class Message: pass
class File:
    def __init__(s, path, *a, **k): s.path = path
class PermissionOverwrite:
    def __init__(s, **k): s.__dict__.update(k)
class AllowedMentions:
    def __init__(s, **k): pass
    @staticmethod
    def none(): return AllowedMentions()
class CategoryChannel: pass
class TextChannel: pass
d.Role, d.Member, d.User, d.Guild, d.Message, d.File = Role, Member, User, Guild, Message, File
d.PermissionOverwrite, d.CategoryChannel, d.TextChannel = PermissionOverwrite, CategoryChannel, TextChannel
d.AllowedMentions = AllowedMentions
d.Interaction = type("Interaction", (), {})

abc = types.ModuleType("discord.abc")
class Messageable: pass
class GuildChannel: pass
abc.Messageable, abc.GuildChannel = Messageable, GuildChannel
d.abc = abc; sys.modules["discord.abc"] = abc

class Embed:
    def __init__(s, title=None, description=None, color=None, timestamp=None):
        s.title, s.description, s.color, s.timestamp = title, description, color, timestamp
        s.fields, s._author, s._footer, s._thumb, s._image = [], None, None, None, None
        s.type = "rich"
    def add_field(s, name, value, inline=True): s.fields.append((name, value, inline)); return s
    def set_footer(s, text=None, icon_url=None): s._footer = (text, icon_url); return s
    def set_author(s, name, icon_url=None): s._author = (name, icon_url); return s
    def set_thumbnail(s, url): s._thumb = url; return s
    def set_image(s, url): s._image = url; return s
    def to_dict(s):
        r = {"type": "rich"}
        if s.title: r["title"] = s.title
        if s.description: r["description"] = s.description
        if s.color is not None: r["color"] = s.color
        if s.timestamp: r["timestamp"] = 1
        if s.fields: r["fields"] = s.fields
        if s._author: r["author"] = 1
        if s._footer: r["footer"] = 1
        if s._thumb: r["thumbnail"] = 1
        if s._image: r["image"] = 1
        return r
d.Embed = Embed

class SelectOption:
    def __init__(s, label, value=None, description=None, emoji=None, default=False):
        s.label, s.value, s.description, s.emoji, s.default = label, value, description, emoji, default

ui = types.ModuleType("discord.ui")
class _Decl:
    def __init__(s, fn, kw): s.fn, s.kw = fn, kw
def button(**kw): return lambda fn: _Decl(fn, kw)
class Item:
    callback = None
    view = None
class Button(Item):
    def __init__(s, label=None, style=None, emoji=None, row=None, disabled=False, custom_id=None, url=None):
        assert not (url and custom_id), "link buttons have no custom_id"
        if url: style = ButtonStyle.link
        s.label, s.style, s.emoji, s.row, s.disabled, s.custom_id, s.url = label, style, emoji, row, disabled, custom_id, url
class DynamicItem(Item):
    def __class_getitem__(cls, item): return cls
    def __init_subclass__(cls, template=None, **kw): cls.template = template
    def __init__(s, item): s.item, s.row, s.custom_id = item, item.row, item.custom_id
class Select(Item):
    def __init__(s, placeholder=None, min_values=1, max_values=1, options=None, row=None, disabled=False,
                 custom_id=None, **kw):
        assert min_values <= max_values, "min_values > max_values"
        assert options is None or 1 <= len(options) <= 25, "bad option count"
        s.placeholder, s.min_values, s.max_values, s.options, s.row = placeholder, min_values, max_values, options, row
        s.disabled, s.custom_id, s.values = disabled, custom_id, []
class RoleSelect(Select): pass
class UserSelect(Select): pass
class ChannelSelect(Select): pass
class TextInput(Item):
    def __init__(s, label, style=None, required=True, placeholder=None, default=None, max_length=None):
        assert max_length is None or max_length <= 4000
        s.label, s.required, s.default, s.max_length = label, required, default, max_length
        s.value = default or ""
class View:
    def __init__(s, timeout=180):
        s.timeout, s.children = timeout, []
        for name in dir(type(s)):
            decl = getattr(type(s), name, None)
            if isinstance(decl, _Decl):
                b = Button(**{k: v for k, v in decl.kw.items() if k in ("label","style","emoji","row","custom_id")})
                async def cb(i, fn=decl.fn, self=s, b=b): return await fn(self, i, b)
                b.callback = cb; s.children.append(b); setattr(s, name, b)
    def add_item(s, item):
        if getattr(item, "row", None) is not None: assert 0 <= item.row <= 4, f"row {item.row}"
        item.view = s; s.children.append(item)
    def stop(s): pass
class Modal:
    def __init_subclass__(cls, title=None, **kw): cls._title = title
    def __init__(s, title=None, **kw): s.title = title or getattr(type(s), "_title", None); s.children = []
    def add_item(s, item): s.children.append(item)
ui.View, ui.Modal, ui.Button, ui.Select, ui.DynamicItem = View, Modal, Button, Select, DynamicItem
ui.RoleSelect, ui.UserSelect, ui.ChannelSelect, ui.TextInput, ui.button = RoleSelect, UserSelect, ChannelSelect, TextInput, button
d.ui = ui; d.SelectOption = SelectOption; sys.modules["discord.ui"] = ui

utils = types.ModuleType("discord.utils")
utils.utcnow = lambda: datetime.datetime.now(datetime.timezone.utc)
d.utils = utils; sys.modules["discord.utils"] = utils

ac = types.ModuleType("discord.app_commands")
class Choice:
    def __init__(s, name, value): s.name, s.value = name, value
class Group:
    def __init__(s, name, description, parent=None, guild_only=False):
        s.name, s.cmds = name, {}
        if parent: parent.cmds[name] = s
    def command(s, name, description):
        def deco(fn): fn.__desc__ = description; s.cmds[name] = fn; return fn
        return deco
ac.Choice, ac.Group = Choice, Group
ac.describe = lambda **k: (lambda fn: fn)
ac.autocomplete = lambda **k: (lambda fn: (setattr(fn, "__autocomplete__", k), fn)[1])
ac.AppCommandError = Exception
d.app_commands = ac; sys.modules["discord.app_commands"] = ac
ext = types.ModuleType("discord.ext"); cmds = types.ModuleType("discord.ext.commands")
cmds.Bot = type("Bot", (), {}); cmds.when_mentioned = None
ext.commands = cmds; d.ext = ext
sys.modules["discord.ext"] = ext; sys.modules["discord.ext.commands"] = cmds
sys.modules["discord"] = d
dotenv = types.ModuleType("dotenv"); dotenv.load_dotenv = lambda *a, **k: None
sys.modules["dotenv"] = dotenv
