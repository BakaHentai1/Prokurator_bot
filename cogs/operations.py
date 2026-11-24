"""Операции Легиона.

/операция (или кнопка на панели Консулата) — форма: название, начало, место, задача,
картинка. Бот публикует пост в текущем канале с кнопками «Приду», «Опоздаю», «Не приду»
и списками ответивших.

- Время вводится в часовом поясе TZ_OFFSET (по умолчанию МСК), а показывается
  каждому в его собственном часовом поясе.
- За 30 минут до начала бот упоминает тех, кто придёт или опоздает, в чате Легиона
  (LEGION_CHAT_CHANNEL_ID).
- Через 3 часа после начала запись закрывается, итоговый список уходит в «Архивариум».
- После начала командир отмечает реальную явку кнопкой «Явка» — она идёт в профиль.
- Данные хранятся в отдельном файле data/operations.db.
"""
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.consulate import archive, can_create, has_any, is_admin, parse_duration

log = logging.getLogger("operations")

DB_PATH: Path = config.DATA_DIR / "operations.db"
REMIND_BEFORE = 30 * 60      # напоминание за 30 минут
CLOSE_AFTER = 3 * 3600       # запись закрывается через 3 часа после начала

ANSWERS = {
    "going": ("✅", "Приду", "Придут", discord.ButtonStyle.success),
    "late": ("⏰", "Опоздаю", "Опоздают", discord.ButtonStyle.primary),
    "no": ("❌", "Не приду", "Не придут", discord.ButtonStyle.secondary),
}
STATUS_TITLES = {"open": "", "cancelled": " · ОТМЕНЕНА", "done": " · ЗАВЕРШЕНА"}


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS operations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER,
                message_id  INTEGER,
                title       TEXT    NOT NULL,
                task        TEXT    NOT NULL DEFAULT '',
                place       TEXT    NOT NULL DEFAULT '',
                image       TEXT    NOT NULL DEFAULT '',
                starts_at   INTEGER NOT NULL,
                creator_id  INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open',
                reminded    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attendance (
                op_id     INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                marked_by INTEGER NOT NULL,
                ts        INTEGER NOT NULL,
                PRIMARY KEY (op_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS responses (
                op_id   INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                answer  TEXT    NOT NULL,
                ts      INTEGER NOT NULL,
                PRIMARY KEY (op_id, user_id)
            );
            """
        )
        self.conn.commit()

    def create(self, o: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO operations (title, task, place, image, starts_at, creator_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (o["title"], o["task"], o["place"], o["image"], o["starts_at"], o["creator_id"], int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, oid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM operations WHERE id = ?", (oid,)).fetchone()
        return dict(row) if row else None

    def set(self, oid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE operations SET {cols} WHERE id = ?", (*fields.values(), oid))
        self.conn.commit()

    def open_ops(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM operations WHERE status = 'open' AND message_id IS NOT NULL ORDER BY starts_at"
        )
        return [dict(r) for r in rows]

    def answer(self, oid: int, user_id: int, answer: str) -> Optional[str]:
        row = self.conn.execute("SELECT answer FROM responses WHERE op_id = ? AND user_id = ?", (oid, user_id)).fetchone()
        self.conn.execute(
            "INSERT OR REPLACE INTO responses (op_id, user_id, answer, ts) VALUES (?, ?, ?, ?)",
            (oid, user_id, answer, int(time.time())),
        )
        self.conn.commit()
        return row["answer"] if row else None

    def set_attendance(self, oid: int, user_ids: list[int], marked_by: int) -> None:
        self.conn.execute("DELETE FROM attendance WHERE op_id = ?", (oid,))
        now = int(time.time())
        for uid in user_ids:
            self.conn.execute("INSERT OR IGNORE INTO attendance VALUES (?, ?, ?, ?)", (oid, uid, marked_by, now))
        self.conn.commit()

    def attended(self, oid: int) -> list[int]:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM attendance WHERE op_id = ? ORDER BY ts",
                                                         (oid,))]

    def roster(self, oid: int) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {key: [] for key in ANSWERS}
        for r in self.conn.execute("SELECT * FROM responses WHERE op_id = ? ORDER BY ts", (oid,)):
            result.setdefault(r["answer"], []).append(r["user_id"])
        return result


db: Optional[DB] = None


# ---------- время ----------

def local_tz() -> timezone:
    return timezone(timedelta(hours=config.TZ_OFFSET))


def tz_name() -> str:
    names = {3: "МСК", 0: "UTC"}
    off = config.TZ_OFFSET
    if off in names:
        return names[off]
    return f"UTC{'+' if off >= 0 else ''}{off:g}"


def parse_start(text: str) -> Optional[int]:
    """«21:00», «22.11 21:00», «22.11.2026 21:00», «через 2ч» → unix-время."""
    text = text.strip().lower()
    now = datetime.now(local_tz())
    if text.startswith("через"):
        seconds = parse_duration(text[5:])
        return int(time.time()) + seconds if seconds else None

    m = re.fullmatch(r"(?:(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s+)?(\d{1,2})[:.](\d{2})", text)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    try:
        if day:
            y = int(year) if year else now.year
            if y < 100:
                y += 2000
            start = now.replace(year=y, month=int(month), day=int(day), hour=int(hour), minute=int(minute),
                                second=0, microsecond=0)
            if not year and start < now - timedelta(days=1):
                start = start.replace(year=start.year + 1)
        else:
            start = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            if start <= now:
                start += timedelta(days=1)
    except ValueError:
        return None
    ts = int(start.timestamp())
    if ts < time.time() or ts > time.time() + 60 * 86400:
        return None
    return ts


# ---------- права ----------

def can_create_op(member: discord.abc.User) -> bool:
    """Операции объявляют Принцепс, Консулы, Сенат и Легаты (и администраторы)."""
    return can_create(member) or (bool(config.LEGATE_ROLE_ID) and has_any(member, {config.LEGATE_ROLE_ID}))


# ---------- оформление ----------

def op_link(o: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{o['channel_id']}/{o['message_id']}"


def _mentions(ids: list[int], limit: int = 700) -> str:
    text = " ".join(f"<@{u}>" for u in ids)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return text or "—"


def op_embed(o: dict) -> discord.Embed:
    status = o["status"]
    color = {"open": discord.Color.dark_red(), "cancelled": discord.Color.dark_grey(),
             "done": discord.Color.dark_gold()}[status]
    lines = []
    if o["task"]:
        lines.append(o["task"][:1500])
        lines.append("")
    head = f"🕒 <t:{o['starts_at']}:f> · <t:{o['starts_at']}:R>"
    if o["place"]:
        head += f"\n📍 {o['place']}"
    head += f" · 🎺 <@{o['creator_id']}>"
    lines.append(head)
    roster = db.roster(o["id"])
    for key, (emoji, _, plural, _) in ANSWERS.items():
        ids = roster.get(key, [])
        if ids or key != "no":
            lines.append(f"{emoji} **{len(ids)}** {_mentions(ids) if ids else ''}".rstrip())
    came = db.attended(o["id"])
    if came:
        lines.append(f"🎖️ Пришли **{len(came)}** {_mentions(came)}")
    e = discord.Embed(title=f"⚔️ {o['title']}{STATUS_TITLES[status]}", description="\n".join(lines)[:4000],
                      color=color)
    if o["image"]:
        e.set_image(url=o["image"])
    return e


class OpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"op:(?P<oid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, oid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: Optional[int] = None) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, row=row,
                                           custom_id=f"op:{oid}:{act}"))
        self.oid = oid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["oid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.act == "att":
            await open_attendance(interaction, self.oid)
        elif self.act == "cancel":
            await cancel_op(interaction, self.oid)
        elif self.act in ANSWERS:
            await respond(interaction, self.oid, self.act)


def op_view(o: dict) -> discord.ui.View:
    """Одна строка: три ответа и две маленькие служебные кнопки (📋 явка, 🚫 отмена)."""
    view = discord.ui.View(timeout=None)
    for key, (emoji, label, _, style) in ANSWERS.items():
        view.add_item(OpButton(o["id"], key, label, emoji, style, row=0))
    view.add_item(OpButton(o["id"], "att", None, "📋", discord.ButtonStyle.secondary, row=0))
    view.add_item(OpButton(o["id"], "cancel", None, "🚫", discord.ButtonStyle.secondary, row=0))
    return view


def done_view(o: dict) -> discord.ui.View:
    """После закрытия записи остаётся только кнопка явки."""
    view = discord.ui.View(timeout=None)
    view.add_item(OpButton(o["id"], "att", "Явка", "📋", discord.ButtonStyle.secondary))
    return view


# ---------- явка ----------

def can_mark(member: discord.abc.User, o: dict) -> bool:
    return member.id == o["creator_id"] or can_create_op(member)


class AttendanceView(discord.ui.View):
    """Командир отмечает, кто реально пришёл."""

    def __init__(self, o: dict, message: discord.Message) -> None:
        super().__init__(timeout=900)
        self.oid = o["id"]
        self.message = message
        roster = db.roster(o["id"])
        signed = roster["going"] + roster["late"]
        already = set(db.attended(o["id"]))
        self.chosen: set[int] = set(already) if already else set(signed)
        self.extra: set[int] = {u for u in already if u not in signed}
        if signed:
            names = {}
            guild = message.guild
            for uid in signed[:25]:
                m = guild.get_member(uid) if guild else None
                names[uid] = m.display_name if m else str(uid)
            self.signed_select.options = [
                discord.SelectOption(label=names[uid][:100], value=str(uid), default=uid in self.chosen,
                                     emoji="✅" if uid in roster["going"] else "⏰")
                for uid in signed[:25]
            ]
            self.signed_select.max_values = len(self.signed_select.options)
        else:
            self.remove_item(self.signed_select)

    def summary(self) -> str:
        people = sorted(self.chosen | self.extra)
        listed = " ".join(f"<@{u}>" for u in people) or "никто"
        return (
            "📋 **Явка на операцию.** Отметьте записавшихся, кто реально пришёл; "
            "пришедших без записи добавьте во втором списке. Затем «Сохранить».\n"
            f"Сейчас отмечено ({len(people)}): {listed}"
        )[:2000]

    @discord.ui.select(placeholder="Пришли из записавшихся", min_values=0, options=[discord.SelectOption(label="—")],
                       row=0)
    async def signed_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.chosen = {int(v) for v in select.values}
        for opt in select.options:
            opt.default = opt.value in select.values
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Пришли без записи (необязательно)", min_values=0,
                       max_values=25, row=1)
    async def extra_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.extra = {u.id for u in select.values if not u.bot}
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Сохранить явку", emoji="💾", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        o = db.get(self.oid)
        if o is None or not can_mark(interaction.user, o):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        people = sorted(self.chosen | self.extra)
        db.set_attendance(self.oid, people, interaction.user.id)
        self.stop()
        await interaction.response.edit_message(content=f"✅ Явка сохранена: {len(people)} чел.", view=None)
        o = db.get(self.oid)
        try:
            await self.message.edit(embed=op_embed(o))
        except discord.HTTPException:
            pass
        await archive(interaction.client,
                      f"📋 <t:{int(time.time())}:f> · {interaction.user.mention} отметил явку на операции №{o['id']} "
                      f"«{o['title']}»: {len(people)} чел. · {op_link(o)}")


async def open_attendance(interaction: discord.Interaction, oid: int) -> None:
    o = db.get(oid)
    if o is None or o["status"] == "cancelled":
        await interaction.response.send_message("Операция отменена.", ephemeral=True)
        return
    if not can_mark(interaction.user, o):
        await interaction.response.send_message("⛔ Явку отмечает командир операции.", ephemeral=True)
        return
    if time.time() < o["starts_at"] - 15 * 60:
        await interaction.response.send_message("Явку можно отметить ближе к началу операции.", ephemeral=True)
        return
    view = AttendanceView(o, interaction.message)
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


# ---------- действия ----------

async def respond(interaction: discord.Interaction, oid: int, answer: str) -> None:
    o = db.get(oid)
    if o is None or o["status"] != "open":
        await interaction.response.send_message("Запись на эту операцию закрыта.", ephemeral=True)
        return
    previous = db.answer(oid, interaction.user.id, answer)
    emoji, label, _, _ = ANSWERS[answer]
    text = f"{emoji} Записано: **{label}**." if previous != answer else f"Вы уже отметили: {emoji} **{label}**."
    if answer in ("going", "late"):
        text += "\nЗа 30 минут до начала напомню в чате Легиона."
    await interaction.response.edit_message(embed=op_embed(o))
    await interaction.followup.send(text, ephemeral=True)


async def legion_chat(client: discord.Client, fallback_id: int) -> Optional[discord.abc.Messageable]:
    channel_id = config.LEGION_CHAT_CHANNEL_ID or fallback_id
    try:
        return client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    except discord.HTTPException:
        log.error("Канал %s недоступен", channel_id)
        return None


async def cancel_op(interaction: discord.Interaction, oid: int) -> None:
    o = db.get(oid)
    if o is None or o["status"] != "open":
        await interaction.response.send_message("Операция уже закрыта.", ephemeral=True)
        return
    if interaction.user.id != o["creator_id"] and not is_admin(interaction.user):
        await interaction.response.send_message(
            "⛔ Отменить операцию может только её командир или администратор.", ephemeral=True
        )
        return
    db.set(oid, status="cancelled")
    o = db.get(oid)
    await interaction.response.edit_message(embed=op_embed(o), view=None)
    await interaction.followup.send("🚫 Операция отменена.", ephemeral=True)

    roster = db.roster(oid)
    notify = roster["going"] + roster["late"]
    if notify:
        channel = await legion_chat(interaction.client, o["channel_id"])
        if channel:
            from cogs.janitor import later
            later(await channel.send(
                " ".join(f"<@{u}>" for u in notify)
                + f"\n🚫 Операция **{o['title']}** (<t:{o['starts_at']}:f>) **отменена**. {op_link(o)}",
                allowed_mentions=discord.AllowedMentions(users=True),
            ), 6 * 3600)
    await archive(interaction.client,
                  f"🚫 <t:{int(time.time())}:f> · {interaction.user.mention} отменил операцию №{oid} "
                  f"«{o['title']}» · {op_link(o)}")
    log.info("Операция %s отменена (%s)", oid, interaction.user)


async def remind(client: discord.Client, o: dict) -> None:
    roster = db.roster(o["id"])
    people = roster["going"] + roster["late"]
    channel = await legion_chat(client, o["channel_id"])
    if channel is None:
        return
    mentions = " ".join(f"<@{u}>" for u in people)
    text = (
        f"⚔️ **Операция «{o['title']}» начинается <t:{o['starts_at']}:R>** (<t:{o['starts_at']}:t>)"
        + (f" · {o['place']}" if o["place"] else "")
        + f"\n{op_link(o)}"
    )
    if mentions:
        text = mentions + "\n" + text
    else:
        text += "\nПока никто не записался."
    from cogs.janitor import later
    later(await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=True)),
          max(int(o["starts_at"] - time.time()), 0) + 2 * 3600)  # уберём через 2 ч после начала
    log.info("Напоминание об операции %s отправлено", o["id"])


async def finish(client: discord.Client, o: dict) -> None:
    db.set(o["id"], status="done")
    o = db.get(o["id"])
    try:
        channel = client.get_channel(o["channel_id"]) or await client.fetch_channel(o["channel_id"])
        await channel.get_partial_message(o["message_id"]).edit(embed=op_embed(o), view=done_view(o))
    except discord.HTTPException as e:
        log.error("Не удалось закрыть пост операции %s: %s", o["id"], e)
    await archive(client, f"📜 Итоги записи на операцию №{o['id']} · {op_link(o)}", op_embed(o))
    log.info("Операция %s завершена", o["id"])


# ---------- создание ----------

class OperationForm(discord.ui.Modal, title="Новая операция"):
    """Шаг 1: текст. Шаг 2 (OpSetupView): день и время из меню."""

    def __init__(self) -> None:
        super().__init__()
        self.op_title = discord.ui.TextInput(label="Название", max_length=100,
                                             placeholder="Например: Штурм Abandoned Ward")
        self.place = discord.ui.TextInput(label="Место (гекс / регион)", max_length=100, required=False,
                                          placeholder="Например: Deadlands / Abandoned Ward")
        self.task = discord.ui.TextInput(label="Задача и сбор", style=discord.TextStyle.paragraph, max_length=1500,
                                         required=False, placeholder="Что делаем, где сбор, что взять с собой")
        self.image = discord.ui.TextInput(label="Ссылка на картинку (необязательно)", max_length=400,
                                          required=False, placeholder="https://…")
        for item in (self.op_title, self.place, self.task, self.image):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_create_op(interaction.user):
            await interaction.response.send_message("⛔ Операции объявляют Принцепс, Консулы, Сенат и Легаты.",
                                                    ephemeral=True)
            return
        image = self.image.value.strip()
        if image and not image.startswith(("http://", "https://")):
            image = ""
        draft = {"title": self.op_title.value.strip(), "task": self.task.value.strip(),
                 "place": self.place.value.strip(), "image": image}
        view = OpSetupView(draft)
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class OpSetupView(discord.ui.View):
    def __init__(self, draft: dict) -> None:
        super().__init__(timeout=900)
        from cogs.timepick import add_when
        self.draft = draft
        self.done = False
        add_when(self, "start", 0, 1)

    def summary(self) -> str:
        from cogs.timepick import when_text
        return (f"⚔️ **{self.draft['title']}**\nНачало: {when_text(self)}\n\n"
                "Выберите день и время и нажмите «Объявить».")

    async def on_when(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Объявить", emoji="⚔️", style=discord.ButtonStyle.success, row=2)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.timepick import when_ok, when_ts
        if self.done:
            return
        err = when_ok(self)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if not can_create_op(interaction.user):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Публикую…", view=None)
        result = await publish_op(interaction, dict(self.draft, starts_at=when_ts(self)))
        await interaction.edit_original_response(content=result)


async def publish_op(interaction: discord.Interaction, d: dict) -> str:
    oid = db.create({**d, "creator_id": interaction.user.id})
    o = db.get(oid)
    ping = f"<@&{config.MEMBER_ROLE_ID}>" if config.MEMBER_ROLE_ID else None
    message = None
    for attempt in range(2):
        try:
            message = await interaction.channel.send(content=ping, embed=op_embed(o), view=op_view(o),
                                                     allowed_mentions=discord.AllowedMentions(roles=True))
            break
        except discord.HTTPException as e:
            if attempt == 0 and o["image"]:  # чаще всего ломается плохая ссылка на картинку
                db.set(oid, image="")
                o = db.get(oid)
                continue
            db.set(oid, status="cancelled")
            return f"Не удалось опубликовать: {e.text or e}"
    db.set(oid, channel_id=message.channel.id, message_id=message.id)
    o = db.get(oid)
    await archive(
        interaction.client,
        f"⚔️ <t:{int(time.time())}:f> · {interaction.user.mention} объявил операцию №{oid} "
        f"«{o['title']}» на <t:{o['starts_at']}:f>" + (f" · {o['place']}" if o["place"] else "")
        + f" · {message.jump_url}",
    )
    log.info("Операция %s объявлена (%s)", oid, interaction.user)
    return f"✅ Операция №{oid} объявлена: {message.jump_url}"


class Operations(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @tasks.loop(minutes=1)
    async def ticker(self) -> None:
        now = time.time()
        for o in db.open_ops():
            try:
                if not o["reminded"] and o["starts_at"] - REMIND_BEFORE <= now < o["starts_at"] + 3600:
                    db.set(o["id"], reminded=1)
                    await remind(self.bot, o)
                if now >= o["starts_at"] + CLOSE_AFTER:
                    await finish(self.bot, o)
            except Exception:
                log.exception("Ошибка при обработке операции %s", o["id"])

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="операция", description="Объявить операцию в этом канале")
    @app_commands.guild_only()
    async def new_op(self, interaction: discord.Interaction) -> None:
        if not can_create_op(interaction.user):
            await interaction.response.send_message("⛔ Операции объявляют Принцепс, Консулы, Сенат и Легаты.", ephemeral=True)
            return
        await interaction.response.send_modal(OperationForm())


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_dynamic_items(OpButton)
    await bot.add_cog(Operations(bot))
