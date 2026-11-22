"""Префектура: объекты Легиона, сборы на добычу, учёт ресурсов.

Права (Фабрум — исполнитель, офицеры — управление):
  Объекты:  добавить / сменить ответственного / снять — офицеры; «Обслужил» — Фабрумы и офицеры.
  Сборы:    объявить — офицеры; «Иду / Не смогу» — все легионеры.
  Ресурсы:  обновить количество — Фабрумы и офицеры; цели (сколько нужно) — офицеры.
Данные — в data/prefecture.db (таблицы facilities, gatherings, gathering_people, resources).
"""
import logging
import re
import sqlite3
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

import config
from cogs.consulate import has_any, is_admin, officer_roles

log = logging.getLogger("industry")

FACILITY_WARN = 2 * 3600  # предупреждать за 2 часа до конца обслуживания
GATHER_REMIND = 15 * 60   # напоминание участникам сбора за 15 минут

RESOURCES = [
    ("salvage", "♻️", "Скрап (Salvage)"),
    ("components", "⚙️", "Компоненты (Components)"),
    ("sulfur", "🟡", "Сера (Sulfur)"),
    ("coal", "⚫", "Уголь (Coal)"),
    ("oil", "🛢️", "Нефть (Oil)"),
    ("petrol", "⛽", "Бензин (Petrol)"),
    ("bmats", "🧱", "Бматы (Basic Materials)"),
    ("emats", "🔌", "Эматы (Explosive Materials)"),
    ("hemats", "💣", "Хематы (Heavy Explosive)"),
    ("rmats", "🔩", "Рматы (Refined Materials)"),
    ("concrete", "🏗️", "Бетон (Concrete)"),
]
RES = {key: (emoji, name) for key, emoji, name in RESOURCES}


# ---------- база ----------

class DB:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS facilities (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id   INTEGER NOT NULL,
                message_id   INTEGER,
                name         TEXT    NOT NULL,
                kind         TEXT    NOT NULL DEFAULT '',
                place        TEXT    NOT NULL DEFAULT '',
                owner_id     INTEGER NOT NULL,
                interval_s   INTEGER NOT NULL,
                expires_at   INTEGER NOT NULL,
                serviced_by  INTEGER,
                serviced_at  INTEGER,
                status       TEXT    NOT NULL DEFAULT 'active',   -- active / removed
                stage        INTEGER NOT NULL DEFAULT 0            -- 1: предупреждён, 2: истёк
            );
            CREATE TABLE IF NOT EXISTS facility_log (
                facility_id INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                ts          INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gatherings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER,
                what        TEXT    NOT NULL,
                place       TEXT    NOT NULL DEFAULT '',
                starts_at   INTEGER NOT NULL,
                comment     TEXT    NOT NULL DEFAULT '',
                author_id   INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open',      -- open / cancelled / done
                reminded    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS gathering_people (
                gathering_id INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                answer       TEXT    NOT NULL,                     -- going / no
                ts           INTEGER NOT NULL,
                PRIMARY KEY (gathering_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS resources (
                key         TEXT PRIMARY KEY,
                amount      INTEGER NOT NULL DEFAULT 0,
                target      INTEGER NOT NULL DEFAULT 0,
                updated_by  INTEGER,
                updated_at  INTEGER
            );
            """
        )
        self.conn.commit()

    # объекты
    def fac_create(self, f: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO facilities (channel_id, name, kind, place, owner_id, interval_s, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f["channel_id"], f["name"], f["kind"], f["place"], f["owner_id"], f["interval_s"],
             int(time.time()) + f["interval_s"]))
        self.conn.commit()
        return cur.lastrowid

    def fac(self, fid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM facilities WHERE id = ?", (fid,)).fetchone()
        return dict(row) if row else None

    def fac_set(self, fid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE facilities SET {cols} WHERE id = ?", (*fields.values(), fid))
        self.conn.commit()

    def fac_active(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM facilities WHERE status = 'active' AND message_id IS NOT NULL ORDER BY expires_at")]

    def fac_service(self, fid: int, user_id: int, interval_s: int) -> None:
        now = int(time.time())
        self.fac_set(fid, expires_at=now + interval_s, interval_s=interval_s, serviced_by=user_id, serviced_at=now,
                     stage=0)
        self.conn.execute("INSERT INTO facility_log VALUES (?, ?, ?)", (fid, user_id, now))
        self.conn.commit()

    def services_by(self, user_id: int, since: int = 0) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM facility_log WHERE user_id = ? AND ts >= ?",
                                 (user_id, since)).fetchone()[0]

    # сборы
    def gat_create(self, g: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO gatherings (channel_id, what, place, starts_at, comment, author_id) VALUES (?, ?, ?, ?, ?, ?)",
            (g["channel_id"], g["what"], g["place"], g["starts_at"], g["comment"], g["author_id"]))
        self.conn.commit()
        return cur.lastrowid

    def gat(self, gid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM gatherings WHERE id = ?", (gid,)).fetchone()
        return dict(row) if row else None

    def gat_set(self, gid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE gatherings SET {cols} WHERE id = ?", (*fields.values(), gid))
        self.conn.commit()

    def gat_open(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM gatherings WHERE status = 'open' AND message_id IS NOT NULL ORDER BY starts_at")]

    def gat_answer(self, gid: int, user_id: int, answer: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO gathering_people VALUES (?, ?, ?, ?)",
                          (gid, user_id, answer, int(time.time())))
        self.conn.commit()

    def gat_people(self, gid: int, answer: str) -> list[int]:
        return [r["user_id"] for r in self.conn.execute(
            "SELECT user_id FROM gathering_people WHERE gathering_id = ? AND answer = ? ORDER BY ts", (gid, answer))]

    # ресурсы
    def resources(self) -> dict[str, dict]:
        rows = {r["key"]: dict(r) for r in self.conn.execute("SELECT * FROM resources")}
        return {key: rows.get(key, {"key": key, "amount": 0, "target": 0, "updated_by": None, "updated_at": None})
                for key, _, _ in RESOURCES}

    def res_set(self, key: str, user_id: int, amount: Optional[int] = None, target: Optional[int] = None) -> None:
        cur = self.resources()[key]
        self.conn.execute(
            "INSERT OR REPLACE INTO resources (key, amount, target, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
            (key, cur["amount"] if amount is None else amount, cur["target"] if target is None else target,
             user_id, int(time.time())))
        self.conn.commit()


db: Optional[DB] = None


# ---------- права ----------

def is_officer(member: discord.abc.User) -> bool:
    return is_admin(member) or has_any(member, officer_roles() | ({config.PRINCEPS_ROLE_ID} - {0}))


def is_worker(member: discord.abc.User) -> bool:
    return is_officer(member) or (bool(config.FABRUM_ROLE_ID) and has_any(member, {config.FABRUM_ROLE_ID}))


def is_legion(member: discord.abc.User) -> bool:
    from cogs.consulate import legion_roles
    return is_admin(member) or has_any(member, legion_roles())


async def refuse(interaction: discord.Interaction, who: str) -> None:
    text = f"⛔ Это могут только {who}."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def dm(client: discord.Client, user_id: int, text: str) -> None:
    user = client.get_user(user_id)
    if user is None:
        return
    try:
        await user.send(text)
    except discord.HTTPException:
        pass


def link(row: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{row['channel_id']}/{row['message_id']}"


def hours_text(seconds: int) -> str:
    h = seconds / 3600
    return f"{h:g} ч" if h >= 1 else f"{seconds // 60} мин"


# ================= ОБЪЕКТЫ =================

def fac_embed(f: dict) -> discord.Embed:
    now = time.time()
    if f["status"] != "active":
        state, color = "🗑️ Снят с учёта", discord.Color.dark_grey()
    elif f["expires_at"] <= now:
        state, color = "🔴 Обслуживание закончилось!", discord.Color.red()
    elif f["expires_at"] - now <= FACILITY_WARN:
        state, color = "🟡 Скоро нужно обслужить", discord.Color.gold()
    else:
        state, color = "🟢 Работает", discord.Color.green()
    e = discord.Embed(title=f"🏭 ОБЪЕКТ №{f['id']} · {f['name']}", color=color)
    if f["kind"]:
        e.add_field(name="Тип", value=f["kind"], inline=True)
    if f["place"]:
        e.add_field(name="Где", value=f["place"], inline=True)
    e.add_field(name="Ответственный", value=f"<@{f['owner_id']}>", inline=True)
    if f["status"] == "active":
        e.add_field(name="Хватит до", value=f"<t:{f['expires_at']}:f>\n<t:{f['expires_at']}:R>", inline=True)
    e.add_field(name="Обслуживать каждые", value=hours_text(f["interval_s"]), inline=True)
    if f["serviced_by"]:
        e.add_field(name="Последний раз обслужил", value=f"<@{f['serviced_by']}> · <t:{f['serviced_at']}:R>",
                    inline=True)
    e.add_field(name="Состояние", value=state, inline=False)
    return e


class FacButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fac:(?P<fid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, fid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"fac:{fid}:{act}"))
        self.fid = fid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["fid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        f = db.fac(self.fid)
        if f is None or f["status"] != "active":
            await interaction.response.send_message("Объект снят с учёта.", ephemeral=True)
            return
        if self.act == "svc":
            if not is_worker(interaction.user):
                await refuse(interaction, "Фабрумы и офицеры")
                return
            await interaction.response.send_modal(ServiceModal(f, interaction.message))
        elif self.act == "owner":
            if not is_officer(interaction.user):
                await refuse(interaction, "офицеры")
                return
            await interaction.response.send_message("Новый ответственный:", view=FacOwnerView(f["id"], interaction.message),
                                                    ephemeral=True)
        elif self.act == "remove":
            if not is_officer(interaction.user):
                await refuse(interaction, "офицеры")
                return
            db.fac_set(f["id"], status="removed")
            await interaction.response.edit_message(embed=fac_embed(db.fac(f["id"])), view=None)


def fac_view(f: dict) -> Optional[discord.ui.View]:
    if f["status"] != "active":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(FacButton(f["id"], "svc", "Обслужил", "⛽", discord.ButtonStyle.success))
    view.add_item(FacButton(f["id"], "owner", "Ответственный", "👤"))
    view.add_item(FacButton(f["id"], "remove", "Снять с учёта", "🗑️", discord.ButtonStyle.danger))
    return view


class ServiceModal(discord.ui.Modal, title="Объект обслужен"):
    def __init__(self, f: dict, message: discord.Message) -> None:
        super().__init__()
        self.fid = f["id"]
        self.message = message
        self.duration = discord.ui.TextInput(label="На сколько теперь хватит", max_length=20,
                                             default=hours_text(f["interval_s"]).replace(" ", ""),
                                             placeholder="Например: 12ч, 1д, 8ч 30м")
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.consulate import parse_duration
        if not is_worker(interaction.user):
            await refuse(interaction, "Фабрумы и офицеры")
            return
        seconds = parse_duration(self.duration.value.replace("мин", "м"))
        if seconds is None:
            await interaction.response.send_message("Не понял время. Примеры: `12ч`, `1д`, `8ч 30м`.", ephemeral=True)
            return
        db.fac_service(self.fid, interaction.user.id, seconds)
        await interaction.response.edit_message(embed=fac_embed(db.fac(self.fid)))


class FacOwnerView(discord.ui.View):
    def __init__(self, fid: int, message: discord.Message) -> None:
        super().__init__(timeout=300)
        self.fid = fid
        self.message = message

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Кто отвечает за объект")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        if not is_officer(interaction.user):
            await refuse(interaction, "офицеры")
            return
        db.fac_set(self.fid, owner_id=select.values[0].id)
        await interaction.response.edit_message(content=f"✅ Ответственный: {select.values[0].mention}", view=None)
        try:
            await self.message.edit(embed=fac_embed(db.fac(self.fid)))
        except discord.HTTPException:
            pass


class FacilityForm(discord.ui.Modal, title="Новый объект Легиона"):
    name = discord.ui.TextInput(label="Название", max_length=80, placeholder="Например: Нефтезавод «Север»")
    kind = discord.ui.TextInput(label="Тип", max_length=60, required=False,
                                placeholder="Фабрика, нефтезавод, генератор, MPF…")
    place = discord.ui.TextInput(label="Где (гекс / регион)", max_length=100, required=False)
    interval = discord.ui.TextInput(label="На сколько хватает топлива/обслуживания", max_length=20,
                                    placeholder="Например: 12ч, 1д")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.consulate import parse_duration
        if not is_officer(interaction.user):
            await refuse(interaction, "офицеры")
            return
        seconds = parse_duration(self.interval.value)
        if seconds is None:
            await interaction.response.send_message("Не понял время. Примеры: `12ч`, `1д`, `8ч 30м`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        fid = db.fac_create({"channel_id": interaction.channel.id, "name": self.name.value.strip(),
                             "kind": self.kind.value.strip(), "place": self.place.value.strip(),
                             "owner_id": interaction.user.id, "interval_s": seconds})
        f = db.fac(fid)
        try:
            message = await interaction.channel.send(embed=fac_embed(f), view=fac_view(f),
                                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            db.fac_set(fid, status="removed")
            await interaction.followup.send(f"Не удалось опубликовать: {e.text or e}", ephemeral=True)
            return
        db.fac_set(fid, message_id=message.id)
        await interaction.followup.send(
            f"✅ Объект №{fid} добавлен. Ответственный — вы; сменить можно кнопкой «👤 Ответственный».",
            ephemeral=True)


def fac_list_embed() -> discord.Embed:
    items = db.fac_active()
    now = time.time()
    lines = []
    for f in items[:25]:
        mark = "🔴" if f["expires_at"] <= now else ("🟡" if f["expires_at"] - now <= FACILITY_WARN else "🟢")
        place = f" · {f['place'][:40]}" if f["place"] else ""
        lines.append(f"{mark} **№{f['id']} {f['name']}**{place} — <@{f['owner_id']}> · до <t:{f['expires_at']}:R> · "
                     f"[открыть]({link(f)})")
    return discord.Embed(title=f"🏭 Объекты Легиона: {len(items)}",
                         description="\n".join(lines)[:4000] or "Объектов пока нет.", color=discord.Color.orange())


async def fac_tick(client: discord.Client) -> None:
    now = time.time()
    for f in db.fac_active():
        try:
            if f["stage"] < 1 and f["expires_at"] - FACILITY_WARN <= now < f["expires_at"]:
                db.fac_set(f["id"], stage=1)
                await dm(client, f["owner_id"], f"🟡 Объект №{f['id']} «{f['name']}» нужно обслужить — "
                                                f"хватит до <t:{f['expires_at']}:t>. {link(f)}")
            elif f["stage"] < 2 and now >= f["expires_at"]:
                db.fac_set(f["id"], stage=2)
                channel = client.get_channel(f["channel_id"])
                if channel:
                    ping = f"<@{f['owner_id']}>"
                    if config.FABRUM_ROLE_ID:
                        ping += f" <@&{config.FABRUM_ROLE_ID}>"
                    await channel.send(f"{ping}\n🔴 Объект №{f['id']} **{f['name']}** остался без обслуживания! "
                                       f"{link(f)}",
                                       allowed_mentions=discord.AllowedMentions(users=True, roles=True))
            else:
                continue
            channel = client.get_channel(f["channel_id"])
            if channel:
                await channel.get_partial_message(f["message_id"]).edit(embed=fac_embed(db.fac(f["id"])))
        except Exception:
            log.exception("Ошибка объекта %s", f["id"])


# ================= СБОРЫ НА ДОБЫЧУ =================

def gat_embed(g: dict) -> discord.Embed:
    state = {"open": "", "cancelled": " · ОТМЕНЁН", "done": " · ЗАВЕРШЁН"}[g["status"]]
    color = discord.Color.dark_green() if g["status"] == "open" else discord.Color.dark_grey()
    e = discord.Embed(title=f"⛏️ СБОР НА ДОБЫЧУ №{g['id']}{state}", description=g["comment"] or None, color=color)
    e.add_field(name="Что добываем", value=g["what"], inline=True)
    if g["place"]:
        e.add_field(name="Где", value=g["place"], inline=True)
    e.add_field(name="Когда", value=f"<t:{g['starts_at']}:f>\n<t:{g['starts_at']}:R>", inline=True)
    e.add_field(name="Собирает", value=f"<@{g['author_id']}>", inline=True)
    going = db.gat_people(g["id"], "going")
    no = db.gat_people(g["id"], "no")
    e.add_field(name=f"✅ Идут ({len(going)})", value=(" ".join(f"<@{u}>" for u in going) or "—")[:1024], inline=False)
    e.add_field(name=f"❌ Не смогут ({len(no)})", value=(" ".join(f"<@{u}>" for u in no) or "—")[:1024], inline=False)
    return e


class GatButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gat:(?P<gid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, gid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"gat:{gid}:{act}"))
        self.gid = gid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["gid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        g = db.gat(self.gid)
        if g is None or g["status"] != "open":
            await interaction.response.send_message("Сбор закрыт.", ephemeral=True)
            return
        if self.act == "cancel":
            if interaction.user.id != g["author_id"] and not is_officer(interaction.user):
                await refuse(interaction, "тот, кто объявил сбор, и офицеры")
                return
            db.gat_set(g["id"], status="cancelled")
            await interaction.response.edit_message(embed=gat_embed(db.gat(g["id"])), view=None)
            return
        if not is_legion(interaction.user):
            await refuse(interaction, "легионеры")
            return
        db.gat_answer(g["id"], interaction.user.id, "going" if self.act == "go" else "no")
        await interaction.response.edit_message(embed=gat_embed(db.gat(g["id"])))


def gat_view(g: dict) -> Optional[discord.ui.View]:
    if g["status"] != "open":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(GatButton(g["id"], "go", "Иду", "✅", discord.ButtonStyle.success))
    view.add_item(GatButton(g["id"], "no", "Не смогу", "❌"))
    view.add_item(GatButton(g["id"], "cancel", "Отменить", "🚫", discord.ButtonStyle.danger))
    return view


class GatheringForm(discord.ui.Modal, title="Сбор на добычу"):
    what = discord.ui.TextInput(label="Что добываем", max_length=80, placeholder="Скрап, компоненты, сера, нефть…")
    place = discord.ui.TextInput(label="Где", max_length=100, required=False, placeholder="Гекс / регион / поле")
    when = discord.ui.TextInput(label="Когда", max_length=30, placeholder="20:00 · 23.11 19:30 · через 30м")
    comment = discord.ui.TextInput(label="Комментарий (необязательно)", style=discord.TextStyle.paragraph,
                                   max_length=500, required=False, placeholder="Место сбора, что взять, транспорт")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.operations import parse_start, tz_name
        if not is_officer(interaction.user):
            await refuse(interaction, "офицеры")
            return
        starts = parse_start(self.when.value)
        if starts is None:
            await interaction.response.send_message(
                f"Не понял время ({tz_name()}). Примеры: `20:00`, `23.11 19:30`, `через 30м`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid = db.gat_create({"channel_id": interaction.channel.id, "what": self.what.value.strip(),
                             "place": self.place.value.strip(), "starts_at": starts,
                             "comment": self.comment.value.strip(), "author_id": interaction.user.id})
        g = db.gat(gid)
        try:
            message = await interaction.channel.send(embed=gat_embed(g), view=gat_view(g),
                                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            db.gat_set(gid, status="cancelled")
            await interaction.followup.send(f"Не удалось опубликовать: {e.text or e}", ephemeral=True)
            return
        db.gat_set(gid, message_id=message.id)
        await interaction.followup.send(f"✅ Сбор №{gid} объявлен. За 15 минут напомню тем, кто идёт.", ephemeral=True)


async def gat_tick(client: discord.Client) -> None:
    now = time.time()
    for g in db.gat_open():
        try:
            if not g["reminded"] and g["starts_at"] - GATHER_REMIND <= now < g["starts_at"] + 1800:
                db.gat_set(g["id"], reminded=1)
                going = db.gat_people(g["id"], "going")
                channel = client.get_channel(g["channel_id"])
                if channel and going:
                    await channel.send(
                        " ".join(f"<@{u}>" for u in going)
                        + f"\n⛏️ Сбор на добычу «{g['what']}» начинается <t:{g['starts_at']}:R>"
                        + (f" · {g['place']}" if g["place"] else "") + f"\n{link(g)}",
                        allowed_mentions=discord.AllowedMentions(users=True))
            if now >= g["starts_at"] + 3 * 3600:
                db.gat_set(g["id"], status="done")
                channel = client.get_channel(g["channel_id"])
                if channel:
                    await channel.get_partial_message(g["message_id"]).edit(embed=gat_embed(db.gat(g["id"])), view=None)
        except Exception:
            log.exception("Ошибка сбора %s", g["id"])


# ================= РЕСУРСЫ =================

def res_embed() -> discord.Embed:
    data = db.resources()
    lines = []
    short = []
    for key, emoji, name in RESOURCES:
        r = data[key]
        line = f"{emoji} **{name}**: {r['amount']:,}".replace(",", " ")
        if r["target"]:
            pct = min(r["amount"] / r["target"], 1)
            bar = "█" * round(pct * 10) + "░" * (10 - round(pct * 10))
            line += f" / {r['target']:,}".replace(",", " ") + f"\n`{bar}` {pct * 100:.0f}%"
            if r["amount"] < r["target"]:
                short.append(f"{emoji} {name.split(' (')[0]}: {r['target'] - r['amount']:,}".replace(",", " "))
        if r["updated_at"]:
            line += f" · <t:{r['updated_at']}:R>"
        lines.append(line)
    e = discord.Embed(title="📦 РЕСУРСЫ ЛЕГИОНА", description="\n".join(lines)[:4000], color=discord.Color.orange())
    if short:
        e.add_field(name="⚠️ Не хватает", value="\n".join(short)[:1024], inline=False)
    e.set_footer(text="Количество обновляют Фабрумы и офицеры; цели ставят офицеры.")
    return e


class ResourceModal(discord.ui.Modal):
    def __init__(self, key: str, officer: bool) -> None:
        emoji, name = RES[key]
        super().__init__(title=f"{emoji} {name}"[:45])
        self.key = key
        cur = db.resources()[key]
        self.amount = discord.ui.TextInput(label="Сколько сейчас (или +500 / -200)", max_length=12,
                                           default=str(cur["amount"]))
        self.add_item(self.amount)
        self.target = None
        if officer:
            self.target = discord.ui.TextInput(label="Сколько нужно (цель, 0 — без цели)", max_length=12,
                                               required=False, default=str(cur["target"]))
            self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_worker(interaction.user):
            await refuse(interaction, "Фабрумы и офицеры")
            return
        cur = db.resources()[self.key]
        raw = self.amount.value.replace(" ", "").strip()
        try:
            if raw.startswith(("+", "-")):
                amount = max(cur["amount"] + int(raw), 0)
            else:
                amount = int(raw)
        except ValueError:
            await interaction.response.send_message("Нужно число, например `1500`, `+500` или `-200`.", ephemeral=True)
            return
        target = None
        if self.target is not None and self.target.value.strip():
            try:
                target = max(int(self.target.value.replace(" ", "")), 0)
            except ValueError:
                await interaction.response.send_message("Цель — число.", ephemeral=True)
                return
            if not is_officer(interaction.user):
                target = None
        db.res_set(self.key, interaction.user.id, amount=amount, target=target)
        await interaction.response.edit_message(embed=res_embed(), view=ResourceView(interaction.user))


class ResourceView(discord.ui.View):
    def __init__(self, viewer: discord.abc.User) -> None:
        super().__init__(timeout=600)
        self.officer = is_officer(viewer)
        if not is_worker(viewer):
            self.remove_item(self.pick)
        else:
            self.pick.options = [discord.SelectOption(label=name, value=key, emoji=emoji)
                                 for key, emoji, name in RESOURCES]

    @discord.ui.select(placeholder="Обновить ресурс…", options=[discord.SelectOption(label="—")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not is_worker(interaction.user):
            await refuse(interaction, "Фабрумы и офицеры")
            return
        await interaction.response.send_modal(ResourceModal(select.values[0], is_officer(interaction.user)))


# ================= ког =================

class Industry(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @tasks.loop(minutes=1)
    async def ticker(self) -> None:
        await fac_tick(self.bot)
        await gat_tick(self.bot)

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    global db
    from cogs import prefecture
    db = DB(prefecture.db.conn)
    bot.add_dynamic_items(FacButton, GatButton)
    await bot.add_cog(Industry(bot))
