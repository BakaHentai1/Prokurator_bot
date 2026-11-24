"""Префектура: производственные заказы.

Создавать и брать заказы могут Фабрумы и офицеры (и администраторы).
Заказ: что нужно, куда доставить (можно номер склада из Секретариума), срок, приоритет.
Статусы: 🆕 Новый → 🙋 Взят → 🏭 В производстве → 🚚 Отправлен → ✅ Доставлен (или 🚫 Отменён).
Срочные заказы упоминают роль Фабрумов. Выполненные заказы идут в профиль исполнителей.
Очередь MPF: «Поставил в MPF» — что, где, когда готово (можно привязать к заказу).
Бот пишет в личку, когда готово, и напоминает в канале, если не забрали за 2 часа.
/префектура — закреплённая панель. Данные — в отдельном файле data/prefecture.db.
"""
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.consulate import has_any, is_admin, officer_roles

log = logging.getLogger("prefecture")

DB_PATH: Path = config.DATA_DIR / "prefecture.db"

STATUSES = {
    "new": ("🆕 Новый", discord.Color.blurple()),
    "taken": ("🙋 Взят", discord.Color.gold()),
    "production": ("🏭 В производстве", discord.Color.orange()),
    "shipped": ("🚚 Отправлен", discord.Color.teal()),
    "delivered": ("✅ Доставлен", discord.Color.green()),
    "cancelled": ("🚫 Отменён", discord.Color.dark_grey()),
}
PRIORITIES = {
    "urgent": "🔴 Срочно",
    "normal": "🟡 Обычный",
    "low": "🟢 Не горит",
}
OPEN = ("new", "taken", "production", "shipped")


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id    INTEGER NOT NULL,
                message_id    INTEGER,
                items         TEXT    NOT NULL,
                destination   TEXT    NOT NULL DEFAULT '',
                warehouse_id  INTEGER,
                comment       TEXT    NOT NULL DEFAULT '',
                priority      TEXT    NOT NULL DEFAULT 'normal',
                deadline      INTEGER,
                author_id     INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'new',
                updated_at    INTEGER NOT NULL,
                overdue_noted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS order_takers (
                order_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                ts       INTEGER NOT NULL,
                PRIMARY KEY (order_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS order_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                status   TEXT    NOT NULL,
                ts       INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def create(self, o: dict) -> int:
        now = int(time.time())
        cur = self.conn.execute(
            "INSERT INTO orders (channel_id, items, destination, warehouse_id, comment, priority, deadline, author_id,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o["channel_id"], o["items"], o["destination"], o["warehouse_id"], o["comment"], o["priority"],
             o["deadline"], o["author_id"], now, now))
        self.conn.commit()
        return cur.lastrowid

    def get(self, oid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        return dict(row) if row else None

    def set(self, oid: int, **fields) -> None:
        fields.setdefault("updated_at", int(time.time()))
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), oid))
        self.conn.commit()

    def log(self, oid: int, user_id: int, status: str) -> None:
        self.conn.execute("INSERT INTO order_log (order_id, user_id, status, ts) VALUES (?, ?, ?, ?)",
                          (oid, user_id, status, int(time.time())))
        self.conn.commit()

    def take(self, oid: int, user_id: int) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO order_takers VALUES (?, ?, ?)", (oid, user_id, int(time.time())))
        self.conn.commit()
        return cur.rowcount > 0

    def drop(self, oid: int, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM order_takers WHERE order_id = ? AND user_id = ?", (oid, user_id))
        self.conn.commit()
        return cur.rowcount > 0

    def takers(self, oid: int) -> list[int]:
        return [r["user_id"] for r in self.conn.execute(
            "SELECT user_id FROM order_takers WHERE order_id = ? ORDER BY ts", (oid,))]

    def open_orders(self) -> list[dict]:
        marks = ",".join("?" * len(OPEN))
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({marks}) AND message_id IS NOT NULL "
            "ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
            "deadline IS NULL, deadline, id", OPEN)]

    def my_orders(self, user_id: int) -> list[dict]:
        marks = ",".join("?" * len(OPEN))
        return [dict(r) for r in self.conn.execute(
            f"SELECT o.* FROM orders o JOIN order_takers t ON t.order_id = o.id "
            f"WHERE t.user_id = ? AND o.status IN ({marks}) ORDER BY o.deadline IS NULL, o.deadline", (user_id, *OPEN))]

    def delivered_count(self, user_id: int, since: int = 0) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM orders o JOIN order_takers t ON t.order_id = o.id "
            "WHERE t.user_id = ? AND o.status = 'delivered' AND o.updated_at >= ?", (user_id, since)).fetchone()[0]


db: Optional[DB] = None


def prefecture_channel_id() -> Optional[int]:
    """Канал Префектуры: где опубликована панель, иначе — где последний раз создавали заказ/MPF/объект."""
    row = db.conn.execute("SELECT value FROM meta WHERE key = 'channel_id'").fetchone()
    if row:
        return int(row[0])
    for table in ("orders", "mpf_jobs", "facilities", "gatherings"):
        try:
            r = db.conn.execute(f"SELECT channel_id FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            continue
        if r:
            return int(r[0])
    return None


# ---------- права ----------

def is_worker(member: discord.abc.User) -> bool:
    """Фабрумы и офицеры (и администраторы)."""
    roles = set(officer_roles())
    for r in (config.FABRUM_ROLE_ID, config.PRINCEPS_ROLE_ID):
        if r:
            roles.add(r)
    return is_admin(member) or has_any(member, roles)


def is_officer(member: discord.abc.User) -> bool:
    """Офицеры (Центурион и выше) и администраторы."""
    return is_admin(member) or has_any(member, officer_roles())


def can_cancel(member: discord.abc.User, o: dict) -> bool:
    return member.id == o["author_id"] or is_admin(member) or has_any(member, officer_roles())


async def no_rights(interaction: discord.Interaction, text: str = "⛔ Это доступно Фабрумам и офицерам.") -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ---------- оформление ----------

def order_link(o: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{o['channel_id']}/{o['message_id']}"


def overdue(o: dict) -> bool:
    return o["status"] in OPEN and o["deadline"] is not None and o["deadline"] < time.time()


def destination_text(o: dict) -> str:
    text = o["destination"] or "—"
    if o["warehouse_id"]:
        try:
            from cogs import warehouses
            wh = warehouses.db.get(o["warehouse_id"]) if warehouses.db else None
            if wh:
                link = warehouses.jump_url(wh)
                place = f"№{wh['id']} · {wh['name']} — {wh['region']}" + (f" / {wh['coords']}" if wh["coords"] else "")
                text = f"[{place}]({link})" if link else place
        except Exception:
            pass
    return text


NEXT_STEP = {
    "new": ("prod", "В производство", "🏭"),
    "taken": ("prod", "В производство", "🏭"),
    "production": ("ship", "Отправлено", "🚚"),
    "shipped": ("done", "Доставлено", "✅"),
}


def order_embed(o: dict) -> discord.Embed:
    label, color = STATUSES[o["status"]]
    if overdue(o):
        color = discord.Color.red()
    lines = [o["items"][:1200], ""]
    lines.append(f"📍 {destination_text(o)}")
    meta = f"{label} · 🧾 <@{o['author_id']}>"
    if o["deadline"]:
        meta += f" · {'🔴 ' if overdue(o) else ''}⏳ <t:{o['deadline']}:R>"
    lines.append(meta)
    takers = db.takers(o["id"])
    if takers:
        lines.append("🙋 " + " ".join(f"<@{u}>" for u in takers)[:600])
    if o["comment"]:
        lines.append(f"💬 {o['comment'][:300]}")
    prio = PRIORITIES[o["priority"]].split()[0]
    return discord.Embed(title=f"{prio} Заказ №{o['id']}", description="\n".join(lines)[:4000], color=color)


class OrderButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ord:(?P<oid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, oid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: Optional[int] = None) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, row=row,
                                           custom_id=f"ord:{oid}:{act}"))
        self.oid = oid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["oid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await on_order_button(interaction, self.oid, self.act)


def order_view(o: dict) -> Optional[discord.ui.View]:
    """Одна строка: «Беру», следующий шаг и маленькие ↩️ / 🚫."""
    if o["status"] not in OPEN:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(OrderButton(o["id"], "take", "Беру", "🙋", discord.ButtonStyle.success, 0))
    act, label, emoji = NEXT_STEP[o["status"]]
    view.add_item(OrderButton(o["id"], act, label, emoji, discord.ButtonStyle.primary, 0))
    view.add_item(OrderButton(o["id"], "drop", None, "↩️", discord.ButtonStyle.secondary, 0))
    view.add_item(OrderButton(o["id"], "cancel", None, "🚫", discord.ButtonStyle.secondary, 0))
    return view


async def on_order_button(interaction: discord.Interaction, oid: int, act: str) -> None:
    o = db.get(oid)
    if o is None or o["status"] not in OPEN:
        await interaction.response.send_message("Заказ уже закрыт.", ephemeral=True)
        return
    user = interaction.user

    if act == "cancel":
        if not can_cancel(user, o):
            await no_rights(interaction, "⛔ Отменить заказ может заказчик или офицер.")
            return
        db.set(oid, status="cancelled")
        db.log(oid, user.id, "cancelled")
    else:
        if not is_worker(user):
            await no_rights(interaction)
            return
        takers = db.takers(oid)
        if act == "take":
            if not db.take(oid, user.id):
                await interaction.response.send_message("Вы уже взяли этот заказ.", ephemeral=True)
                return
            if o["status"] == "new":
                db.set(oid, status="taken")
            db.log(oid, user.id, "taken")
            if user.id != o["author_id"]:
                await dm(interaction.client, o["author_id"],
                         f"🙋 {user.display_name} взял ваш заказ №{oid}. {order_link(o)}")
        elif act == "drop":
            if not db.drop(oid, user.id):
                await interaction.response.send_message("Вы не брали этот заказ.", ephemeral=True)
                return
            if not db.takers(oid):
                db.set(oid, status="new")
            db.log(oid, user.id, "dropped")
        else:
            new = {"prod": "production", "ship": "shipped", "done": "delivered"}[act]
            if user.id not in takers and user.id != o["author_id"] and not has_any(user, officer_roles()) \
                    and not is_admin(user):
                await no_rights(interaction, "⛔ Статус меняют исполнители, заказчик или офицеры. Сначала «Беру».")
                return
            if user.id not in takers and new != "delivered":
                db.take(oid, user.id)  # кто двигает производство — тот исполнитель
            db.set(oid, status=new)
            db.log(oid, user.id, new)
            if new in ("shipped", "delivered") and user.id != o["author_id"]:
                what = "отправлен" if new == "shipped" else "доставлен"
                await dm(interaction.client, o["author_id"], f"🚚 Ваш заказ №{oid} {what}. {order_link(o)}")

    o = db.get(oid)
    await interaction.response.edit_message(embed=order_embed(o), view=order_view(o))


async def dm(client: discord.Client, user_id: int, text: str) -> None:
    user = client.get_user(user_id)
    if user is None:
        return
    try:
        await user.send(text)
    except discord.HTTPException:
        pass


# ---------- создание ----------

def parse_deadline(text: str) -> Optional[int]:
    from cogs.operations import parse_start
    from cogs.profiles import parse_until
    return parse_start(text) or parse_until(text)


def find_warehouse(text: str) -> Optional[int]:
    """«№3», «склад 3», «3» → номер склада, если такой есть."""
    m = re.fullmatch(r"\s*(?:склад\s*)?[№#]?\s*(\d{1,5})\s*", text.lower())
    if not m:
        return None
    try:
        from cogs import warehouses
        wid = int(m.group(1))
        return wid if warehouses.db and warehouses.db.get(wid) else None
    except Exception:
        return None


class OrderForm(discord.ui.Modal, title="Новый производственный заказ"):
    items = discord.ui.TextInput(label="Что нужно", style=discord.TextStyle.paragraph, max_length=1000,
                                 placeholder="Например:\n100 ящиков 7.62\n20 ящиков бинтов")
    destination = discord.ui.TextInput(label="Куда доставить", max_length=150,
                                       placeholder="Номер склада (№3) или место: Deadlands / Abandoned Ward")
    comment = discord.ui.TextInput(label="Комментарий (необязательно)", style=discord.TextStyle.paragraph,
                                   max_length=500, required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deadline = None
        draft = {
            "channel_id": interaction.channel.id,
            "items": self.items.value.strip(),
            "destination": self.destination.value.strip(),
            "warehouse_id": find_warehouse(self.destination.value),
            "comment": self.comment.value.strip(),
            "priority": "normal",
            "deadline": deadline,
            "author_id": interaction.user.id,
        }
        view = OrderSetupView(draft)
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class OrderSetupView(discord.ui.View):
    def __init__(self, draft: dict) -> None:
        super().__init__(timeout=900)
        from cogs.timepick import add_when
        self.draft = draft
        self.done = False
        add_when(self, "deadline", 1, 2)

    def summary(self) -> str:
        from cogs.timepick import when_text
        d = self.draft
        where = d["destination"] + (" (склад найден ✅)" if d["warehouse_id"] else "")
        return (f"🏭 **Заказ:** {d['items'][:300]}\n**Куда:** {where}\n**Приоритет:** {PRIORITIES[d['priority']]}\n"
                f"**Срок:** {when_text(self)}\n\n"
                "Выберите приоритет и срок и нажмите «Опубликовать». Срочные заказы упоминают Фабрумов.")[:2000]

    async def on_when(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(placeholder="Приоритет: обычный", options=[
        discord.SelectOption(label="Срочно", value="urgent", emoji="🔴"),
        discord.SelectOption(label="Обычный", value="normal", emoji="🟡"),
        discord.SelectOption(label="Не горит", value="low", emoji="🟢"),
    ], row=0)
    async def priority(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["priority"] = select.values[0]
        for o in select.options:
            o.default = o.value == self.draft["priority"]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Опубликовать", emoji="🏭", style=discord.ButtonStyle.success, row=3)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.timepick import when_ok, when_ts
        if self.done:
            return
        err = when_ok(self)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        self.draft["deadline"] = when_ts(self)
        if not is_officer(interaction.user):
            await no_rights(interaction, "⛔ Заказы создают офицеры.")
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Публикую…", view=None)
        oid = db.create(self.draft)
        o = db.get(oid)
        ping = None
        mentions = discord.AllowedMentions.none()
        if o["priority"] == "urgent" and config.FABRUM_ROLE_ID:
            ping = f"<@&{config.FABRUM_ROLE_ID}> 🔴 срочный заказ!"
            mentions = discord.AllowedMentions(roles=True)
        try:
            message = await interaction.channel.send(content=ping, embed=order_embed(o), view=order_view(o),
                                                     allowed_mentions=mentions)
        except discord.HTTPException as e:
            db.set(oid, status="cancelled")
            await interaction.edit_original_response(content=f"Не удалось опубликовать: {e.text or e}")
            return
        db.set(oid, message_id=message.id)
        db.log(oid, interaction.user.id, "new")
        await interaction.edit_original_response(content=f"✅ Заказ №{oid} опубликован: {message.jump_url}")
        log.info("Заказ %s создан (%s)", oid, interaction.user)


def list_embed(items: list[dict], title: str) -> discord.Embed:
    lines = []
    for o in items[:20]:
        first = o["items"].splitlines()[0][:80]
        when = f" · до <t:{o['deadline']}:R>" if o["deadline"] else ""
        mark = "🔴 " if overdue(o) else ""
        lines.append(f"{mark}**№{o['id']}** {PRIORITIES[o['priority']].split()[0]} {first} — "
                     f"{STATUSES[o['status']][0]}{when} · [открыть]({order_link(o)})")
    text = "\n".join(lines) or "Открытых заказов нет."
    if len(items) > 20:
        text += f"\n…и ещё {len(items) - 20}"
    return discord.Embed(title=f"{title}: {len(items)}", description=text[:4000], color=discord.Color.orange())


# ---------- MPF: очередь массового производства ----------

class MpfDB:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mpf_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER,
                user_id     INTEGER NOT NULL,
                items       TEXT    NOT NULL,
                place       TEXT    NOT NULL DEFAULT '',
                order_id    INTEGER,
                created_at  INTEGER NOT NULL,
                ready_at    INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'queued',   -- queued / collected / cancelled
                stage       INTEGER NOT NULL DEFAULT 0,          -- 1: готово, 2: лежит 2+ ч
                closed_at   INTEGER
            );
            """
        )
        self.conn.commit()

    def create(self, j: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO mpf_jobs (channel_id, user_id, items, place, order_id, created_at, ready_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (j["channel_id"], j["user_id"], j["items"], j["place"], j["order_id"], int(time.time()), j["ready_at"]))
        self.conn.commit()
        return cur.lastrowid

    def get(self, jid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM mpf_jobs WHERE id = ?", (jid,)).fetchone()
        return dict(row) if row else None

    def set(self, jid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE mpf_jobs SET {cols} WHERE id = ?", (*fields.values(), jid))
        self.conn.commit()

    def queued(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mpf_jobs WHERE status = 'queued' AND message_id IS NOT NULL ORDER BY ready_at")]


mpf: Optional[MpfDB] = None
MPF_LATE = 2 * 3600  # через столько после готовности — повторное напоминание


def mpf_link(j: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{j['channel_id']}/{j['message_id']}"


def mpf_embed(j: dict) -> discord.Embed:
    now = time.time()
    if j["status"] == "collected":
        state, color = "📦 Забрано", discord.Color.green()
    elif j["status"] == "cancelled":
        state, color = "🚫 Отменено", discord.Color.dark_grey()
    elif j["ready_at"] <= now:
        state, color = "✅ Готово — пора забирать!", discord.Color.gold()
    else:
        state, color = "⏳ Производится", discord.Color.orange()
    e = discord.Embed(title=f"🏗️ MPF №{j['id']}", color=color)
    e.add_field(name="Что", value=j["items"][:1024], inline=False)
    if j["place"]:
        e.add_field(name="Где MPF", value=j["place"][:1024], inline=True)
    e.add_field(name="Поставил", value=f"<@{j['user_id']}>", inline=True)
    if j["status"] == "queued":
        e.add_field(name="Готово", value=f"<t:{j['ready_at']}:t> · <t:{j['ready_at']}:R>", inline=True)
    elif j["closed_at"]:
        e.add_field(name="Закрыто", value=f"<t:{j['closed_at']}:f>", inline=True)
    e.add_field(name="Статус", value=state, inline=False)
    if j["order_id"]:
        o = db.get(j["order_id"])
        if o and o["message_id"]:
            e.add_field(name="Для заказа", value=f"[№{o['id']}]({order_link(o)})", inline=True)
    return e


class MpfButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mpf:(?P<jid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, jid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"mpf:{jid}:{act}"))
        self.jid = jid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["jid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        j = mpf.get(self.jid)
        if j is None or j["status"] != "queued":
            await interaction.response.send_message("Эта запись уже закрыта.", ephemeral=True)
            return
        if interaction.user.id != j["user_id"] and not has_any(interaction.user, officer_roles()) \
                and not is_admin(interaction.user):
            await no_rights(interaction, "⛔ Это может тот, кто поставил, или офицер.")
            return
        status = "collected" if self.act == "got" else "cancelled"
        mpf.set(j["id"], status=status, closed_at=int(time.time()))
        await interaction.response.edit_message(embed=mpf_embed(mpf.get(j["id"])), view=None)
        if status == "collected" and j["order_id"]:
            await interaction.followup.send(
                f"📦 Отмечено. Не забудьте нажать «🚚 Отправлено» на заказе №{j['order_id']}, когда повезёте.",
                ephemeral=True)


def mpf_view(j: dict) -> Optional[discord.ui.View]:
    if j["status"] != "queued":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(MpfButton(j["id"], "got", "Забрал", "📦", discord.ButtonStyle.success))
    view.add_item(MpfButton(j["id"], "cancel", "Отменить", "🚫", discord.ButtonStyle.secondary))
    return view


class MpfForm(discord.ui.Modal, title="Поставил в MPF"):
    items = discord.ui.TextInput(label="Что поставил", style=discord.TextStyle.paragraph, max_length=500,
                                 placeholder="Например: 9 ящиков 7.62")
    place = discord.ui.TextInput(label="Где MPF (гекс / город)", max_length=100, required=False,
                                 placeholder="Например: Deadlands / Abandoned Ward")
    ready = discord.ui.TextInput(label="Готово через", max_length=20, placeholder="Например: 3ч, 2ч 30м, 45м")
    order = discord.ui.TextInput(label="Для заказа № (необязательно)", max_length=10, required=False,
                                 placeholder="Например: 12")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.consulate import parse_duration
        if not is_worker(interaction.user):
            await no_rights(interaction)
            return
        seconds = parse_duration(self.ready.value)
        if seconds is None:
            await interaction.response.send_message("Не понял время. Примеры: `3ч`, `2ч 30м`, `45м`.", ephemeral=True)
            return
        order_id = None
        raw = re.sub(r"\D", "", self.order.value)
        if raw:
            o = db.get(int(raw))
            if o is None or o["status"] not in OPEN:
                await interaction.response.send_message(f"Открытого заказа №{raw} нет.", ephemeral=True)
                return
            order_id = o["id"]
        await interaction.response.defer(ephemeral=True, thinking=True)
        jid = mpf.create({"channel_id": interaction.channel.id, "user_id": interaction.user.id,
                          "items": self.items.value.strip(), "place": self.place.value.strip(),
                          "order_id": order_id, "ready_at": int(time.time()) + seconds})
        j = mpf.get(jid)
        try:
            message = await interaction.channel.send(embed=mpf_embed(j), view=mpf_view(j),
                                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            mpf.set(jid, status="cancelled")
            await interaction.followup.send(f"Не удалось опубликовать: {e.text or e}", ephemeral=True)
            return
        mpf.set(jid, message_id=message.id)
        if order_id:  # заказ переходит в производство, поставивший — исполнитель
            db.take(order_id, interaction.user.id)
            o = db.get(order_id)
            if o["status"] in ("new", "taken"):
                db.set(order_id, status="production")
                db.log(order_id, interaction.user.id, "production")
            o = db.get(order_id)
            try:
                channel = interaction.client.get_channel(o["channel_id"])
                if channel:
                    await channel.get_partial_message(o["message_id"]).edit(embed=order_embed(o), view=order_view(o))
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            f"🏗️ Записано: MPF №{jid}, готово <t:{j['ready_at']}:t> (<t:{j['ready_at']}:R>). Напомню в личку.",
            ephemeral=True)


def mpf_list_embed() -> discord.Embed:
    items = mpf.queued()
    now = time.time()
    lines = []
    for j in items[:25]:
        first = j["items"].splitlines()[0][:70]
        mark = "✅" if j["ready_at"] <= now else "⏳"
        place = f" · {j['place'][:40]}" if j["place"] else ""
        lines.append(f"{mark} **№{j['id']}** {first}{place} — <@{j['user_id']}> · <t:{j['ready_at']}:R> · "
                     f"[открыть]({mpf_link(j)})")
    text = "\n".join(lines) or "В MPF сейчас ничего не стоит."
    return discord.Embed(title=f"🏗️ Очередь MPF: {len(items)}", description=text[:4000], color=discord.Color.orange())


async def mpf_tick(client: discord.Client) -> None:
    now = time.time()
    for j in mpf.queued():
        try:
            if j["stage"] < 1 and now >= j["ready_at"]:
                mpf.set(j["id"], stage=1)
                await dm(client, j["user_id"], f"✅ MPF №{j['id']} готово — пора забирать: {j['items'][:200]}\n"
                                               f"{mpf_link(j)}")
                channel = client.get_channel(j["channel_id"])
                if channel:
                    await channel.get_partial_message(j["message_id"]).edit(embed=mpf_embed(mpf.get(j["id"])))
            elif j["stage"] < 2 and now >= j["ready_at"] + MPF_LATE:
                mpf.set(j["id"], stage=2)
                channel = client.get_channel(j["channel_id"])
                if channel:
                    await channel.send(
                        f"<@{j['user_id']}> 📦 MPF №{j['id']} готово уже больше 2 часов — заберите, пожалуйста. "
                        f"{mpf_link(j)}",
                        allowed_mentions=discord.AllowedMentions(users=True))
        except Exception:
            log.exception("Ошибка MPF %s", j["id"])


# ---------- панель ----------

class PrefecturePanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Новый заказ", emoji="➕", style=discord.ButtonStyle.success, custom_id="pref:new")
    async def new(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await no_rights(interaction, "⛔ Заказы создают офицеры. Фабрумы берут их кнопкой «Беру».")
            return
        await interaction.response.send_modal(OrderForm())

    @discord.ui.button(label="Открытые заказы", emoji="📋", style=discord.ButtonStyle.primary, custom_id="pref:list")
    async def open_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=list_embed(db.open_orders(), "📋 Открытые заказы"),
                                                ephemeral=True)

    @discord.ui.button(label="Поставил в MPF", emoji="🏗️", style=discord.ButtonStyle.success, custom_id="pref:mpf",
                       row=1)
    async def mpf_add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_worker(interaction.user):
            await no_rights(interaction)
            return
        await interaction.response.send_modal(MpfForm())

    @discord.ui.button(label="Очередь MPF", emoji="⏳", style=discord.ButtonStyle.primary, custom_id="pref:mpflist",
                       row=1)
    async def mpf_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=mpf_list_embed(), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Мои заказы", emoji="🙋", style=discord.ButtonStyle.secondary, custom_id="pref:mine")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=list_embed(db.my_orders(interaction.user.id), "🙋 Ваши заказы"),
                                                ephemeral=True)

    @discord.ui.button(label="Объекты", emoji="🏭", style=discord.ButtonStyle.primary, custom_id="pref:fac", row=2)
    async def facilities(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.industry import fac_list_embed
        await interaction.response.send_message(embed=fac_list_embed(), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Новый объект", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="pref:facnew",
                       row=2)
    async def facility_new(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.industry import FacilityForm
        if not is_officer(interaction.user):
            await no_rights(interaction, "⛔ Объекты добавляют офицеры.")
            return
        await interaction.response.send_modal(FacilityForm())

    @discord.ui.button(label="Сбор на добычу", emoji="⛏️", style=discord.ButtonStyle.success, custom_id="pref:gather",
                       row=3)
    async def gathering(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.industry import GatheringForm
        if not is_officer(interaction.user):
            await no_rights(interaction, "⛔ Сборы объявляют офицеры.")
            return
        await interaction.response.send_modal(GatheringForm())

    @discord.ui.button(label="Ресурсы", emoji="📦", style=discord.ButtonStyle.primary, custom_id="pref:res", row=3)
    async def resources(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.industry import ResourceView, res_embed
        view = ResourceView(interaction.user)
        await interaction.response.send_message(embed=res_embed(), view=view if view.children else None,
                                                ephemeral=True)


    @discord.ui.button(label="Очистка", emoji="⚠️", custom_id="pref:reset", row=4)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.warehouses import can_reset
        if not can_reset(interaction.user):
            await no_rights(interaction, "⛔ Очистка Префектуры доступна только Принцепсу.")
            return
        await interaction.response.send_modal(PrefResetModal())


class PrefResetModal(discord.ui.Modal, title="⚠️ Очистка Префектуры"):
    confirm = discord.ui.TextInput(label="Для подтверждения напишите: ОЧИСТКА", max_length=10)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.warehouses import can_reset
        if not can_reset(interaction.user):
            await no_rights(interaction, "⛔ Очистка Префектуры доступна только Принцепсу.")
            return
        if self.confirm.value.strip().upper() != "ОЧИСТКА":
            await interaction.response.send_message("Очистка отменена: подтверждение не совпало.", ephemeral=True)
            return
        await interaction.response.send_message("⏳ Очищаю Префектуру… Это может занять пару минут.", ephemeral=True)
        text = await reset_prefecture(interaction.client, interaction.user)
        await interaction.edit_original_response(content=text)


async def reset_prefecture(client: discord.Client, user: discord.abc.User) -> str:
    """Закрывает всё открытое в Префектуре и чистит канал (кроме закреплённых сообщений)."""
    note = ""
    try:
        from cogs.backup import send_backup
        if config.AUDIT_CHANNEL_ID:
            await send_backup(client, f"перед очисткой Префектуры ({user})")
            note = "резервная копия — в «Инвентариуме»"
    except Exception as e:
        log.error("Копия перед очисткой Префектуры: %s", e)
        note = "⚠️ резервную копию сделать не удалось"
    now = int(time.time())
    c = db.conn
    counts = {
        "заказов": c.execute(f"UPDATE orders SET status = 'cancelled', updated_at = ? WHERE status IN "
                             f"({','.join('?' * len(OPEN))})", (now, *OPEN)).rowcount,
        "MPF": c.execute("UPDATE mpf_jobs SET status = 'cancelled', closed_at = ? WHERE status = 'queued'",
                         (now,)).rowcount,
    }
    for label, sql in (("объектов", "UPDATE facilities SET status = 'removed' WHERE status = 'active'"),
                       ("сборов", "UPDATE gatherings SET status = 'cancelled' WHERE status = 'open'"),
                       ("ресурсов", "UPDATE resources SET amount = 0, updated_by = NULL, updated_at = NULL "
                                    "WHERE amount != 0")):
        try:
            counts[label] = c.execute(sql).rowcount
        except sqlite3.Error:
            counts[label] = 0
    c.commit()

    deleted = 0
    cid = prefecture_channel_id()
    channel = client.get_channel(cid) if cid else None
    if channel is not None:
        try:
            deleted = len(await channel.purge(limit=3000, check=lambda m: not m.pinned))
        except discord.HTTPException as e:
            note += f"\n⚠️ канал очистить не вышло: {e.text or e} (нужно право «Управлять сообщениями»)"

    summary = ", ".join(f"{k} — {v}" for k, v in counts.items())
    try:
        from cogs.consulate import archive
        await archive(client, f"⚠️ <t:{now}:f> · {user.mention} выполнил **очистку Префектуры**: закрыто {summary}; "
                              f"удалено сообщений — {deleted}.")
    except Exception:
        pass
    log.info("Очистка Префектуры (%s): %s, сообщений %d", user, summary, deleted)
    return (f"✅ Префектура очищена. Закрыто: {summary}. Удалено сообщений в канале: **{deleted}**. "
            f"Закреплённая панель осталась." + (f"\n{note}" if note else ""))


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🏭 ПРЕФЕКТУРА",
        description="Заказы, MPF, объекты, добыча и ресурсы — в меню ниже.\n"
                    "Офицеры создают, Фабрумы исполняют.",
        color=discord.Color.orange(),
    )


class Prefecture(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Обновляет меню закреплённой панели (чтобы появлялись новые пункты)."""
        if getattr(self, "_panel_refreshed", False):
            return
        self._panel_refreshed = True
        cid = prefecture_channel_id()
        channel = self.bot.get_channel(cid) if cid else None
        if channel is None:
            return
        try:
            from cogs.compact import prefecture_panel
            for m in await channel.pins():
                if m.author.id == self.bot.user.id and any(
                        getattr(ch, "custom_id", None) == "pref:menu"
                        for row in m.components for ch in getattr(row, "children", [])):
                    await m.edit(embed=panel_embed(), view=prefecture_panel()())
        except discord.HTTPException as e:
            log.warning("Не удалось обновить панель Префектуры: %s", e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Короткое оповещение в Префектуре, когда кто-то становится Фабрумом."""
        if not config.FABRUM_ROLE_ID or after.guild.id != config.GUILD_ID:
            return
        had = any(r.id == config.FABRUM_ROLE_ID for r in before.roles)
        has = any(r.id == config.FABRUM_ROLE_ID for r in after.roles)
        if had or not has:
            return
        channel_id = prefecture_channel_id()
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(f"🔧 {after.mention} повышен до <@&{config.FABRUM_ROLE_ID}>! "
                               "Добро пожаловать в Префектуру — заказы и MPF ждут.",
                               allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e:
            log.error("Не удалось оповестить о новом Фабруме: %s", e)

    async def cog_load(self) -> None:
        self.ticker.start()
        self.mpf_ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()
        self.mpf_ticker.cancel()

    @tasks.loop(minutes=5)
    async def ticker(self) -> None:
        """Отмечает просроченные заказы и сообщает исполнителям и заказчику."""
        for o in db.open_orders():
            if not overdue(o) or o["overdue_noted"]:
                continue
            db.set(o["id"], overdue_noted=1, updated_at=o["updated_at"])
            people = set(db.takers(o["id"])) | {o["author_id"]}
            for uid in people:
                await dm(self.bot, uid, f"🔴 Заказ №{o['id']} просрочен. {order_link(o)}")
            channel = self.bot.get_channel(o["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(o["message_id"]).edit(embed=order_embed(db.get(o["id"])))
                except discord.HTTPException:
                    pass

    @tasks.loop(minutes=1)
    async def mpf_ticker(self) -> None:
        await mpf_tick(self.bot)

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @mpf_ticker.before_loop
    async def before_mpf(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="mpf", description="Записать, что поставлено в MPF (Фабрумы и офицеры)")
    @app_commands.guild_only()
    async def mpf_cmd(self, interaction: discord.Interaction) -> None:
        if not is_worker(interaction.user):
            await no_rights(interaction)
            return
        await interaction.response.send_modal(MpfForm())

    @app_commands.command(name="заказ", description="Новый производственный заказ (офицеры)")
    @app_commands.guild_only()
    async def new_order(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await no_rights(interaction, "⛔ Заказы создают офицеры. Фабрумы берут их кнопкой «Беру».")
            return
        await interaction.response.send_modal(OrderForm())

    @app_commands.command(name="префектура", description="Опубликовать и закрепить панель Префектуры в этом канале")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user) and not has_any(interaction.user, officer_roles()):
            await no_rights(interaction, "⛔ Недостаточно прав.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from cogs.compact import prefecture_panel
            message = await interaction.channel.send(embed=panel_embed(), view=prefecture_panel()())
            db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('channel_id', ?)", (str(interaction.channel.id),))
            db.conn.commit()
        except discord.HTTPException as e:
            await interaction.followup.send(f"Не удалось опубликовать панель: {e.text or e}", ephemeral=True)
            return
        try:
            await message.pin(reason="Панель Префектуры")
            note = "и закреплена"
        except discord.HTTPException:
            note = "но закрепить не вышло: дайте боту право «Закреплять сообщения» или закрепите вручную"
        await interaction.followup.send(f"✅ Панель опубликована {note}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    global db, mpf
    db = DB(DB_PATH)
    mpf = MpfDB(db.conn)
    bot.add_dynamic_items(OrderButton, MpfButton)
    bot.add_view(PrefecturePanel())
    await bot.add_cog(Prefecture(bot))
