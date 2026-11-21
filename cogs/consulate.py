"""Консулат: голосования Легиона.

/голосование — форма (вопрос, варианты, срок), затем выбор: кто голосует, тайное
или открытое, упоминать ли Легион. Бот публикует голосование в текущем канале.

- Один человек — один голос, менять голос можно до конца.
- До конца видно только число проголосовавших, без промежуточных итогов.
- По окончании бот сам публикует итоги. В открытом голосовании — кто за что голосовал.
- Данные хранятся в data/consulate.db (отдельный файл, другие данные не затрагиваются).
"""
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("consulate")

DB_PATH: Path = config.DATA_DIR / "consulate.db"
LETTERS = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭", "🇮", "🇯"]
AUDIENCES = {"legion": "⚔️ Весь Легион", "officers": "🎖️ Только офицеры"}
MAX_DAYS = 14


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS polls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER,
                message_id  INTEGER,
                question    TEXT    NOT NULL,
                options     TEXT    NOT NULL,          -- JSON-список вариантов
                creator_id  INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                ends_at     INTEGER NOT NULL,
                secret      INTEGER NOT NULL DEFAULT 1,
                audience    TEXT    NOT NULL DEFAULT 'legion',
                status      TEXT    NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option  INTEGER NOT NULL,
                ts      INTEGER NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            );
            """
        )
        self.conn.commit()

    def create(self, p: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO polls (question, options, creator_id, created_at, ends_at, secret, audience)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p["question"], json.dumps(p["options"], ensure_ascii=False), p["creator_id"],
             int(time.time()), p["ends_at"], int(p["secret"]), p["audience"]),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, pid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM polls WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return None
        p = dict(row)
        p["options"] = json.loads(p["options"])
        return p

    def set(self, pid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE polls SET {cols} WHERE id = ?", (*fields.values(), pid))
        self.conn.commit()

    def due(self) -> list[int]:
        rows = self.conn.execute("SELECT id FROM polls WHERE status = 'open' AND ends_at <= ?", (int(time.time()),))
        return [r["id"] for r in rows]

    def vote(self, pid: int, user_id: int, option: int) -> Optional[int]:
        """Записывает голос. Возвращает прежний вариант (или None)."""
        row = self.conn.execute("SELECT option FROM votes WHERE poll_id = ? AND user_id = ?", (pid, user_id)).fetchone()
        self.conn.execute(
            "INSERT OR REPLACE INTO votes (poll_id, user_id, option, ts) VALUES (?, ?, ?, ?)",
            (pid, user_id, option, int(time.time())),
        )
        self.conn.commit()
        return row["option"] if row else None

    def count(self, pid: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM votes WHERE poll_id = ?", (pid,)).fetchone()[0]

    def votes(self, pid: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM votes WHERE poll_id = ? ORDER BY ts", (pid,))]


db: Optional[DB] = None


# ---------- права ----------

def legion_roles() -> set[int]:
    ids = {
        config.MEMBER_ROLE_ID, config.FABRUM_ROLE_ID, config.CENTURION_ROLE_ID, config.LEGATE_ROLE_ID,
        config.CONSUL_ROLE_ID, config.SENATE_ROLE_ID, config.PRINCEPS_ROLE_ID, *config.REVIEWER_ROLE_IDS,
    }
    return {i for i in ids if i}


def officer_roles() -> set[int]:
    ids = {config.PRINCEPS_ROLE_ID, *config.REVIEWER_ROLE_IDS}
    return {i for i in ids if i}


def is_admin(member: discord.abc.User) -> bool:
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator or member.guild.owner_id == member.id
    )


def has_any(member: discord.abc.User, roles: set[int]) -> bool:
    return isinstance(member, discord.Member) and any(r.id in roles for r in member.roles)


def can_create(member: discord.abc.User) -> bool:
    roles = {i for i in (config.PRINCEPS_ROLE_ID, config.CONSUL_ROLE_ID, config.SENATE_ROLE_ID) if i}
    return is_admin(member) or has_any(member, roles)


def can_vote(member: discord.abc.User, poll: dict) -> bool:
    if is_admin(member):
        return True
    roles = officer_roles() if poll["audience"] == "officers" else legion_roles()
    return has_any(member, roles)


# ---------- оформление ----------

def parse_duration(text: str) -> Optional[int]:
    """«24ч», «2д», «90м», «1д 12ч», «36» (часы) → секунды. Пусто → 24 ч."""
    text = text.strip().lower().replace(",", ".")
    if not text:
        return 24 * 3600
    if re.fullmatch(r"\d+(\.\d+)?", text):
        seconds = float(text) * 3600
    else:
        parts = re.findall(r"(\d+(?:\.\d+)?)\s*(д|дн|дня|дней|d|ч|час|часа|часов|h|м|мин|минут|m)", text)
        if not parts or re.sub(r"[\d.\sа-яa-z]", "", text):
            return None
        seconds = 0.0
        for num, unit in parts:
            mult = 86400 if unit[0] in "дd" else 3600 if unit[0] in "чh" else 60
            seconds += float(num) * mult
    if seconds < 60 or seconds > MAX_DAYS * 86400:
        return None
    return int(seconds)


def poll_embed(p: dict) -> discord.Embed:
    closed = p["status"] == "closed"
    e = discord.Embed(
        title=f"🗳️ ГОЛОСОВАНИЕ КОНСУЛАТА №{p['id']}" + (" · ЗАВЕРШЕНО" if closed else ""),
        description=f"**{p['question']}**\n\n"
        + "\n".join(f"{LETTERS[i]} {opt}" for i, opt in enumerate(p["options"])),
        color=discord.Color.dark_grey() if closed else discord.Color.dark_red(),
    )
    e.add_field(name="Голосуют", value=AUDIENCES[p["audience"]], inline=True)
    e.add_field(name="Тип", value="🔒 Тайное" if p["secret"] else "👁️ Открытое", inline=True)
    e.add_field(name="Проголосовало", value=str(db.count(p["id"])), inline=True)
    if closed:
        e.add_field(name="Завершено", value=f"<t:{p['ends_at']}:f>", inline=False)
    else:
        e.add_field(name="Окончание", value=f"<t:{p['ends_at']}:f> (<t:{p['ends_at']}:R>)", inline=False)
        e.set_footer(text="Голос можно изменить до конца голосования. Итоги — после завершения.")
    return e


def results_embed(p: dict) -> discord.Embed:
    votes = db.votes(p["id"])
    total = len(votes)
    counts = [0] * len(p["options"])
    voters: list[list[int]] = [[] for _ in p["options"]]
    for v in votes:
        if 0 <= v["option"] < len(counts):
            counts[v["option"]] += 1
            voters[v["option"]].append(v["user_id"])

    lines = []
    for i, opt in enumerate(p["options"]):
        pct = counts[i] / total * 100 if total else 0
        bar = "█" * round(pct / 10) + "░" * (10 - round(pct / 10))
        lines.append(f"{LETTERS[i]} **{opt}**\n`{bar}` {counts[i]} ({pct:.0f}%)")

    best = max(counts) if counts else 0
    leaders = [p["options"][i] for i, c in enumerate(counts) if c == best]
    if total == 0:
        verdict = "Никто не проголосовал."
    elif len(leaders) > 1:
        verdict = "⚖️ Ничья: " + ", ".join(f"«{x}»" for x in leaders)
    else:
        verdict = f"🏆 Решение: **{leaders[0]}**"

    e = discord.Embed(
        title=f"📜 ИТОГИ ГОЛОСОВАНИЯ №{p['id']}",
        description=f"**{p['question']}**\n\n" + "\n\n".join(lines) + f"\n\n{verdict}\nВсего голосов: {total}",
        color=discord.Color.gold(),
    )
    if not p["secret"] and total:
        for i, opt in enumerate(p["options"]):
            if voters[i]:
                names = " ".join(f"<@{u}>" for u in voters[i])
                e.add_field(name=f"{LETTERS[i]} {opt[:200]}", value=names[:1024], inline=False)
    e.set_footer(text="Senatus Populusque Legionis")
    return e


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<pid>[0-9]+):(?P<act>[0-9]+|end)"):
    def __init__(self, pid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: Optional[int] = None) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, row=row,
                                           custom_id=f"poll:{pid}:{act}"))
        self.pid = pid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["pid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.act == "end":
            await end_early(interaction, self.pid)
        else:
            await cast_vote(interaction, self.pid, int(self.act))


def poll_view(p: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i, opt in enumerate(p["options"]):
        label = opt if len(opt) <= 70 else opt[:67] + "…"
        view.add_item(PollButton(p["id"], str(i), label, LETTERS[i], discord.ButtonStyle.primary, row=i // 5))
    view.add_item(PollButton(p["id"], "end", "Завершить досрочно", "⏹️", discord.ButtonStyle.danger, row=2))
    return view


# ---------- действия ----------

async def cast_vote(interaction: discord.Interaction, pid: int, option: int) -> None:
    p = db.get(pid)
    if p is None or p["status"] != "open" or p["ends_at"] <= time.time():
        await interaction.response.send_message("Голосование уже завершено.", ephemeral=True)
        return
    if not can_vote(interaction.user, p):
        who = "офицеры" if p["audience"] == "officers" else "легионеры"
        await interaction.response.send_message(f"⛔ В этом голосовании участвуют только {who}.", ephemeral=True)
        return
    previous = db.vote(pid, interaction.user.id, option)
    choice = p["options"][option]
    if previous is None:
        text = f"✅ Ваш голос принят: {LETTERS[option]} **{choice}**"
    elif previous == option:
        text = f"Вы уже голосуете за {LETTERS[option]} **{choice}**."
    else:
        text = f"🔄 Голос изменён на {LETTERS[option]} **{choice}**"
    if p["secret"]:
        text += "\n🔒 Голосование тайное: ваш выбор не увидит никто."
    await interaction.response.send_message(text, ephemeral=True)
    if previous is None:
        try:
            await interaction.message.edit(embed=poll_embed(p))
        except discord.HTTPException:
            pass


async def close_poll(client: discord.Client, pid: int) -> None:
    p = db.get(pid)
    if p is None or p["status"] != "open":
        return
    db.set(pid, status="closed", ends_at=min(p["ends_at"], int(time.time())))
    p = db.get(pid)
    channel = client.get_channel(p["channel_id"])
    if channel is None:
        try:
            channel = await client.fetch_channel(p["channel_id"])
        except discord.HTTPException:
            log.error("Канал голосования %s недоступен", pid)
            return
    try:
        message = channel.get_partial_message(p["message_id"])
        await message.edit(embed=poll_embed(p), view=None)
        await channel.send(embed=results_embed(p), reference=message, mention_author=False,
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.error("Не удалось опубликовать итоги голосования %s: %s", pid, e)
    log.info("Голосование %s завершено", pid)


async def end_early(interaction: discord.Interaction, pid: int) -> None:
    p = db.get(pid)
    if p is None or p["status"] != "open":
        await interaction.response.send_message("Голосование уже завершено.", ephemeral=True)
        return
    if interaction.user.id != p["creator_id"] and not is_admin(interaction.user):
        await interaction.response.send_message(
            "⛔ Завершить досрочно может только автор голосования или администратор.", ephemeral=True
        )
        return
    await interaction.response.send_message("⏹️ Голосование завершено, итоги опубликованы.", ephemeral=True)
    await close_poll(interaction.client, pid)


# ---------- создание ----------

class PollForm(discord.ui.Modal, title="Новое голосование"):
    question = discord.ui.TextInput(label="Вопрос", style=discord.TextStyle.paragraph, max_length=500)
    options = discord.ui.TextInput(
        label="Варианты — каждый с новой строки (2–10)",
        style=discord.TextStyle.paragraph,
        placeholder="За\nПротив\nВоздерживаюсь",
        max_length=1000,
    )
    duration = discord.ui.TextInput(
        label="Длительность",
        placeholder="Пусто — 24ч. Например: 12ч, 2д, 1д 12ч, 90м",
        max_length=20,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        opts = [o.strip() for o in self.options.value.splitlines() if o.strip()]
        if not 2 <= len(opts) <= 10:
            await interaction.response.send_message("Нужно от 2 до 10 вариантов, каждый с новой строки.", ephemeral=True)
            return
        seconds = parse_duration(self.duration.value)
        if seconds is None:
            await interaction.response.send_message(
                f"Не понял длительность. Примеры: 12ч, 2д, 1д 12ч, 90м (до {MAX_DAYS} дней).", ephemeral=True
            )
            return
        draft = {
            "question": self.question.value.strip(),
            "options": [o[:200] for o in opts],
            "seconds": seconds,
            "creator_id": interaction.user.id,
            "audience": "legion",
            "secret": True,
            "ping": True,
        }
        view = PollSetupView(draft)
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class PollSetupView(discord.ui.View):
    def __init__(self, draft: dict) -> None:
        super().__init__(timeout=900)
        self.draft = draft
        self.done = False

    def summary(self) -> str:
        d = self.draft
        hours = d["seconds"] / 3600
        length = f"{hours / 24:g} дн." if hours >= 24 and hours % 24 == 0 else f"{hours:g} ч"
        return (
            f"**Голосование:** {d['question']}\n"
            + "\n".join(f"{LETTERS[i]} {o}" for i, o in enumerate(d["options"]))
            + f"\n\nДлительность: {length} · Голосуют: {AUDIENCES[d['audience']]} · "
            f"{'🔒 Тайное' if d['secret'] else '👁️ Открытое'} · "
            f"{'🔔 с упоминанием Легиона' if d['ping'] else '🔕 без упоминания'}\n\n"
            "Измените настройки при необходимости и нажмите «Опубликовать». "
            "Голосование появится в этом канале."
        )

    @discord.ui.select(
        placeholder="Кто голосует: весь Легион",
        options=[discord.SelectOption(label=label, value=key) for key, label in AUDIENCES.items()],
        row=0,
    )
    async def audience(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["audience"] = select.values[0]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(
        placeholder="Тип: тайное",
        options=[
            discord.SelectOption(label="Тайное — никто не видит, кто как голосовал", value="1", emoji="🔒"),
            discord.SelectOption(label="Открытое — в итогах видно, кто за что", value="0", emoji="👁️"),
        ],
        row=1,
    )
    async def secret(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["secret"] = select.values[0] == "1"
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(
        placeholder="Упомянуть Легион: да",
        options=[
            discord.SelectOption(label="Упомянуть роль голосующих", value="1", emoji="🔔"),
            discord.SelectOption(label="Без упоминания", value="0", emoji="🔕"),
        ],
        row=2,
    )
    async def ping(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["ping"] = select.values[0] == "1"
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Опубликовать", emoji="🗳️", style=discord.ButtonStyle.success, row=3)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.done:
            return
        if not can_create(interaction.user):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Публикую…", view=None)

        d = self.draft
        pid = db.create(dict(d, ends_at=int(time.time()) + d["seconds"]))
        p = db.get(pid)

        content = None
        mentions = discord.AllowedMentions.none()
        if d["ping"]:
            role_ids = sorted(officer_roles()) if d["audience"] == "officers" else [config.MEMBER_ROLE_ID]
            role_ids = [r for r in role_ids if r]
            if role_ids:
                content = " ".join(f"<@&{r}>" for r in role_ids)
                mentions = discord.AllowedMentions(roles=True)
        try:
            message = await interaction.channel.send(content=content, embed=poll_embed(p), view=poll_view(p),
                                                     allowed_mentions=mentions)
        except discord.HTTPException as e:
            db.set(pid, status="closed")
            await interaction.edit_original_response(content=f"Не удалось опубликовать: {e.text or e}")
            return
        db.set(pid, channel_id=message.channel.id, message_id=message.id)
        log.info("Голосование %s создано (%s)", pid, interaction.user)
        await interaction.edit_original_response(content=f"✅ Голосование №{pid} опубликовано: {message.jump_url}")


class Consulate(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.closer.start()

    async def cog_unload(self) -> None:
        self.closer.cancel()

    @tasks.loop(minutes=1)
    async def closer(self) -> None:
        for pid in db.due():
            try:
                await close_poll(self.bot, pid)
            except Exception:
                log.exception("Ошибка при завершении голосования %s", pid)

    @closer.before_loop
    async def before_closer(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="голосование", description="Создать голосование в этом канале")
    @app_commands.guild_only()
    async def new_poll(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await interaction.response.send_message(
                "⛔ Голосования создают Принцепс, Консулы и Сенат.", ephemeral=True
            )
            return
        await interaction.response.send_modal(PollForm())


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_dynamic_items(PollButton)
    await bot.add_cog(Consulate(bot))
