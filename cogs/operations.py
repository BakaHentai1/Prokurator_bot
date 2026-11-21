"""Операции Легиона.

/операция (или кнопка на панели Консулата) — форма: название, начало, место, задача,
картинка. Бот публикует пост в текущем канале с кнопками «Приду», «Опоздаю», «Не приду»
и списками ответивших.

- Время вводится в часовом поясе TZ_OFFSET (по умолчанию МСК), а показывается
  каждому в его собственном часовом поясе.
- За 30 минут до начала бот упоминает тех, кто придёт или опоздает, в чате Легиона
  (LEGION_CHAT_CHANNEL_ID).
- Через 3 часа после начала запись закрывается, итоговый список уходит в «Архивариум».
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
from cogs.consulate import archive, is_admin, has_any, officer_roles, parse_duration

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
    return is_admin(member) or has_any(member, officer_roles())


# ---------- оформление ----------

def op_link(o: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{o['channel_id']}/{o['message_id']}"


def op_embed(o: dict) -> discord.Embed:
    status = o["status"]
    color = {"open": discord.Color.dark_red(), "cancelled": discord.Color.dark_grey(),
             "done": discord.Color.dark_gold()}[status]
    e = discord.Embed(
        title=f"⚔️ ОПЕРАЦИЯ: {o['title']}{STATUS_TITLES[status]}",
        description=o["task"] or None,
        color=color,
    )
    e.add_field(name="Начало", value=f"<t:{o['starts_at']}:f>\n<t:{o['starts_at']}:R>", inline=True)
    if o["place"]:
        e.add_field(name="Место", value=o["place"], inline=True)
    e.add_field(name="Командует", value=f"<@{o['creator_id']}>", inline=True)

    roster = db.roster(o["id"])
    for key, (emoji, _, plural, _) in ANSWERS.items():
        people = roster.get(key, [])
        value = " ".join(f"<@{u}>" for u in people) or "—"
        if len(value) > 1024:
            value = value[:1000].rsplit(" ", 1)[0] + " …"
        e.add_field(name=f"{emoji} {plural} ({len(people)})", value=value, inline=False)
    if o["image"]:
        e.set_image(url=o["image"])
    if status == "open":
        e.set_footer(text="Время показано в вашем часовом поясе. Ответ можно изменить.")
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
        if self.act == "cancel":
            await cancel_op(interaction, self.oid)
        elif self.act in ANSWERS:
            await respond(interaction, self.oid, self.act)


def op_view(o: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for key, (emoji, label, _, style) in ANSWERS.items():
        view.add_item(OpButton(o["id"], key, label, emoji, style, row=0))
    view.add_item(OpButton(o["id"], "cancel", "Отменить операцию", "🚫", discord.ButtonStyle.danger, row=1))
    return view


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
            await channel.send(
                " ".join(f"<@{u}>" for u in notify)
                + f"\n🚫 Операция **{o['title']}** (<t:{o['starts_at']}:f>) **отменена**. {op_link(o)}",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
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
    await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=True))
    log.info("Напоминание об операции %s отправлено", o["id"])


async def finish(client: discord.Client, o: dict) -> None:
    db.set(o["id"], status="done")
    o = db.get(o["id"])
    try:
        channel = client.get_channel(o["channel_id"]) or await client.fetch_channel(o["channel_id"])
        await channel.get_partial_message(o["message_id"]).edit(embed=op_embed(o), view=None)
    except discord.HTTPException as e:
        log.error("Не удалось закрыть пост операции %s: %s", o["id"], e)
    await archive(client, f"📜 Итоги записи на операцию №{o['id']} · {op_link(o)}", op_embed(o))
    log.info("Операция %s завершена", o["id"])


# ---------- создание ----------

class OperationForm(discord.ui.Modal, title="Новая операция"):
    def __init__(self) -> None:
        super().__init__()
        self.op_title = discord.ui.TextInput(label="Название", max_length=100,
                                             placeholder="Например: Штурм Abandoned Ward")
        self.start = discord.ui.TextInput(label=f"Начало ({tz_name()})", max_length=30,
                                          placeholder="21:00 · 23.11 21:00 · через 2ч")
        self.place = discord.ui.TextInput(label="Место (гекс / регион)", max_length=100, required=False,
                                          placeholder="Например: Deadlands / Abandoned Ward")
        self.task = discord.ui.TextInput(label="Задача и сбор", style=discord.TextStyle.paragraph, max_length=1500,
                                         required=False, placeholder="Что делаем, где сбор, что взять с собой")
        self.image = discord.ui.TextInput(label="Ссылка на картинку (необязательно)", max_length=400,
                                          required=False, placeholder="https://…")
        for item in (self.op_title, self.start, self.place, self.task, self.image):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_create_op(interaction.user):
            await interaction.response.send_message("⛔ Операции объявляют офицеры.", ephemeral=True)
            return
        starts_at = parse_start(self.start.value)
        if starts_at is None:
            await interaction.response.send_message(
                f"Не понял время начала: «{self.start.value}». Примеры ({tz_name()}): `21:00`, `23.11 21:00`, "
                "`через 2ч`. Время должно быть в будущем, не дальше 60 дней.",
                ephemeral=True,
            )
            return
        image = self.image.value.strip()
        if image and not image.startswith(("http://", "https://")):
            image = ""
        await interaction.response.defer(ephemeral=True, thinking=True)

        oid = db.create({
            "title": self.op_title.value.strip(), "task": self.task.value.strip(), "place": self.place.value.strip(),
            "image": image, "starts_at": starts_at, "creator_id": interaction.user.id,
        })
        o = db.get(oid)
        ping = f"<@&{config.MEMBER_ROLE_ID}>" if config.MEMBER_ROLE_ID else None
        try:
            message = await interaction.channel.send(
                content=ping, embed=op_embed(o), view=op_view(o),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException as e:
            if image:  # чаще всего ломается плохая ссылка на картинку — пробуем без неё
                db.set(oid, image="")
                o = db.get(oid)
                try:
                    message = await interaction.channel.send(
                        content=ping, embed=op_embed(o), view=op_view(o),
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                except discord.HTTPException as e2:
                    db.set(oid, status="cancelled")
                    await interaction.followup.send(f"Не удалось опубликовать: {e2.text or e2}", ephemeral=True)
                    return
            else:
                db.set(oid, status="cancelled")
                await interaction.followup.send(f"Не удалось опубликовать: {e.text or e}", ephemeral=True)
                return

        db.set(oid, channel_id=message.channel.id, message_id=message.id)
        o = db.get(oid)
        await interaction.followup.send(f"✅ Операция №{oid} объявлена: {message.jump_url}", ephemeral=True)
        await archive(
            interaction.client,
            f"⚔️ <t:{int(time.time())}:f> · {interaction.user.mention} объявил операцию №{oid} "
            f"«{o['title']}» на <t:{o['starts_at']}:f>" + (f" · {o['place']}" if o["place"] else "")
            + f" · {message.jump_url}",
        )
        log.info("Операция %s объявлена (%s)", oid, interaction.user)


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
            await interaction.response.send_message("⛔ Операции объявляют офицеры.", ephemeral=True)
            return
        await interaction.response.send_modal(OperationForm())


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_dynamic_items(OpButton)
    await bot.add_cog(Operations(bot))
