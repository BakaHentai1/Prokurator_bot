"""Панель Легиона — для всех легионеров.

[📢 Актуальное] [🎯 Получить задание] + меню:
  🪪 Мой профиль · 🎯 Специализация · 📈 Путь легионера · 📨 Связь с командованием ·
  💡 Предложение · 📋 Мои задания · 🏖️ Брейквар · ❓ Помощь

- Актуальное: операции, сборы, голосования, выборы, эдикты — где ещё не ответил.
- Получить задание: открытые задания от офицеров (/задание) и автоматические
  (обновить склад, обслужить объект, сбор на добычу), с учётом специализации.
- Специализации на римский лад: «Фрументарий», «Катафрактарий»… (бот создаёт роли сам), видны в профиле.
- Путь легионера: прогресс до следующего звания; пороги — /путь_настроить (офицеры).
- Рапорты командованию: карточка в «Сенате», ответ офицера приходит в личку.
- Предложения: голос легионеров 👍/👎, решение офицеров.
Данные — в отдельном файле data/legion_hub.db.
"""
import logging
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.consulate import has_any, is_admin, legion_roles, officer_roles

log = logging.getLogger("legion")

DB_PATH: Path = config.DATA_DIR / "legion_hub.db"

SPECS = [  # (ключ, значок, римское название)
    ("logist", "🌾", "Фрументарий"),
    ("tank", "🛡️", "Катафрактарий"),
    ("arty", "🏹", "Баллистарий"),
    ("medic", "🩺", "Капсарий"),
    ("infantry", "🗡️", "Гастат"),
    ("engineer", "📐", "Архитект"),
    ("navy", "⚓", "Классиарий"),
    ("scout", "🔭", "Эксплоратор"),
    ("pilot", "🦅", "Аквилифер"),
]
SPEC_HINTS = {
    "logist": "логистика и снабжение",
    "tank": "танки и бронетехника",
    "arty": "артиллерия",
    "medic": "медик",
    "infantry": "пехота",
    "engineer": "инженер: стройка, укрепления",
    "navy": "флот",
    "scout": "разведка",
    "pilot": "пилот",
}
OLD_SPEC_NAMES = {  # прежние названия ролей — переименовываются автоматически
    "logist": "Логист", "tank": "Танкист", "arty": "Артиллерия", "medic": "Медик", "infantry": "Пехота",
    "engineer": "Инженер", "navy": "Флот", "scout": "Разведка",
}
SPEC = {k: (e, n) for k, e, n in SPECS}

REPORT_KINDS = {
    "question": ("❓", "Вопрос"),
    "idea": ("💭", "Пожелание"),
    "complaint": ("⚠️", "Жалоба"),
    "other": ("📝", "Другое"),
}
REPORT_STATUS = {"new": "🆕 Новый", "taken": "🙋 В работе", "closed": "✅ Закрыт"}
PROPOSAL_STATUS = {
    "open": ("🗳️ Обсуждается", discord.Color.blurple()),
    "accepted": ("✅ Принято", discord.Color.green()),
    "progress": ("🔨 В работе", discord.Color.gold()),
    "done": ("🏁 Реализовано", discord.Color.dark_green()),
    "rejected": ("❌ Отклонено", discord.Color.dark_grey()),
}
DEFAULT_PATH = {  # звание → (дней в Легионе, операций с явкой, заданий)
    "fabrum": (14, 2, 3),
    "centurion": (45, 10, 15),
    "legate": (90, 25, 40),
}


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT    NOT NULL,
                details    TEXT    NOT NULL DEFAULT '',
                spec       TEXT    NOT NULL DEFAULT '',
                slots      INTEGER NOT NULL DEFAULT 1,
                deadline   INTEGER,
                author_id  INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'open'          -- open / closed
            );
            CREATE TABLE IF NOT EXISTS job_takers (
                job_id   INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                status   TEXT    NOT NULL DEFAULT 'taken',          -- taken / done / dropped
                ts       INTEGER NOT NULL,
                done_ts  INTEGER,
                PRIMARY KEY (job_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id   INTEGER NOT NULL,
                kind        TEXT    NOT NULL,
                anonymous   INTEGER NOT NULL DEFAULT 0,
                text        TEXT    NOT NULL,
                channel_id  INTEGER,
                message_id  INTEGER,
                status      TEXT    NOT NULL DEFAULT 'new',
                officer_id  INTEGER,
                answer      TEXT    NOT NULL DEFAULT '',
                ts          INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id   INTEGER NOT NULL,
                title       TEXT    NOT NULL,
                text        TEXT    NOT NULL DEFAULT '',
                channel_id  INTEGER,
                message_id  INTEGER,
                status      TEXT    NOT NULL DEFAULT 'open',
                comment     TEXT    NOT NULL DEFAULT '',
                decided_by  INTEGER,
                ts          INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposal_votes (
                proposal_id INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                vote        INTEGER NOT NULL,
                PRIMARY KEY (proposal_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS path (
                rank  TEXT PRIMARY KEY,
                days  INTEGER NOT NULL,
                ops   INTEGER NOT NULL,
                jobs  INTEGER NOT NULL
            );
            """
        )
        for rank, (d, o, j) in DEFAULT_PATH.items():
            self.conn.execute("INSERT OR IGNORE INTO path VALUES (?, ?, ?, ?)", (rank, d, o, j))
        self.conn.commit()

    def meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.conn.commit()

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args)]

    def x(self, sql: str, args: tuple = ()) -> int:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.lastrowid


db: Optional[DB] = None


# ---------- общие ----------

def is_legion(member: discord.abc.User) -> bool:
    return is_admin(member) or has_any(member, legion_roles())


def is_officer(member: discord.abc.User) -> bool:
    return is_admin(member) or has_any(member, officer_roles())


async def say(interaction: discord.Interaction, text: str = None, **kw) -> None:
    kw.setdefault("ephemeral", True)
    kw.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if interaction.response.is_done():
        await interaction.followup.send(text, **kw)
    else:
        await interaction.response.send_message(text, **kw)


async def dm(client: discord.Client, user_id: int, text: str) -> None:
    user = client.get_user(user_id)
    if user is None:
        try:
            user = await client.fetch_user(user_id)
        except discord.HTTPException:
            return
    try:
        await user.send(text[:2000])
    except discord.HTTPException:
        pass


def link(channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{channel_id}/{message_id}"


# ================= 📢 АКТУАЛЬНОЕ =================

def actual_embed(member: discord.Member) -> discord.Embed:
    now = int(time.time())
    uid = member.id
    sections: list[tuple[str, list[str]]] = []

    try:
        from cogs import operations
        if operations.db is not None:
            lines = []
            for o in operations.db.open_ops():
                if o["starts_at"] < now - 3600 or o["starts_at"] > now + 7 * 86400:
                    continue
                row = operations.db.conn.execute(
                    "SELECT answer FROM responses WHERE op_id = ? AND user_id = ?", (o["id"], uid)).fetchone()
                mark = {"going": "✅", "late": "⏰", "no": "❌"}.get(row["answer"] if row else "", "❗")
                lines.append(f"{mark} [{o['title']}]({link(o['channel_id'], o['message_id'])}) — "
                             f"<t:{o['starts_at']}:R>")
            if lines:
                sections.append(("⚔️ Операции", lines))
    except Exception:
        log.exception("Актуальное: операции")

    try:
        from cogs import industry
        if industry.db is not None:
            lines = []
            for g in industry.db.gat_open():
                if g["starts_at"] < now - 1800:
                    continue
                going = uid in industry.db.gat_people(g["id"], "going")
                lines.append(f"{'✅' if going else '▫️'} [⛏️ {g['what']}]({link(g['channel_id'], g['message_id'])})"
                             f" — <t:{g['starts_at']}:R>")
            if lines:
                sections.append(("⛏️ Сборы на добычу", lines))
    except Exception:
        log.exception("Актуальное: сборы")

    votes = []
    try:
        from cogs import consulate
        if consulate.db is not None:
            for p in consulate.db.open_polls():
                voted = consulate.db.conn.execute(
                    "SELECT 1 FROM votes WHERE poll_id = ? AND user_id = ?", (p["id"], uid)).fetchone()
                votes.append(f"{'✔️' if voted else '❗'} [🗳️ {p['question'][:60]}]({link(p['channel_id'], p['message_id'])})"
                             f" — до <t:{p['ends_at']}:R>")
    except Exception:
        log.exception("Актуальное: голосования")
    try:
        from cogs import elections
        if elections.db is not None:
            for e in elections.db.active():
                if e["phase"] == "vote":
                    voted = elections.db.conn.execute(
                        "SELECT 1 FROM ballots WHERE election_id = ? AND voter_id = ?", (e["id"], uid)).fetchone()
                    votes.append(f"{'✔️' if voted else '❗'} [🏛️ Выборы: {e['position']}]"
                                 f"({link(e['channel_id'], e['message_id'])}) — до <t:{e['vote_ends']}:R>")
                else:
                    votes.append(f"🙋 [🏛️ Выдвижение: {e['position']}]({link(e['channel_id'], e['message_id'])})"
                                 f" — до <t:{e['nominate_ends']}:R>")
    except Exception:
        log.exception("Актуальное: выборы")
    if votes:
        sections.append(("🗳️ Голосования и выборы", votes))

    try:
        from cogs import edicts
        if edicts.db is not None:
            lines = []
            rows = edicts.db.conn.execute(
                "SELECT * FROM edicts WHERE status = 'published' AND publish_at >= ? ORDER BY publish_at DESC LIMIT 5",
                (now - 7 * 86400,)).fetchall()
            for r in rows:
                acked = edicts.db.conn.execute("SELECT 1 FROM acks WHERE edict_id = ? AND user_id = ?",
                                               (r["id"], uid)).fetchone()
                mark = "✔️" if (acked or not r["ack"]) else "❗"
                lines.append(f"{mark} [📜 №{r['id']} {r['title'][:60]}]({link(r['channel_id'], r['message_id'])})")
            if lines:
                sections.append(("📜 Эдикты за неделю", lines))
    except Exception:
        log.exception("Актуальное: эдикты")

    e = discord.Embed(title="📢 Актуальное", color=discord.Color.dark_red())
    if not sections:
        e.description = "Сейчас ничего не происходит. Отдыхай, легионер. 🍷"
    for name, lines in sections:
        e.add_field(name=name, value="\n".join(lines)[:1024], inline=False)
    e.set_footer(text="❗ — ждёт вашего ответа")
    return e


# ================= 🎯 ЗАДАНИЯ =================

def my_specs(member: discord.Member) -> set[str]:
    ids = spec_roles()
    held = {r.id for r in member.roles}
    return {k for k, rid in ids.items() if rid in held}


def open_jobs_for(member: discord.Member) -> list[dict]:
    now = int(time.time())
    jobs = db.q("SELECT * FROM jobs WHERE status = 'open' AND (deadline IS NULL OR deadline > ?) ORDER BY id", (now,))
    result = []
    for j in jobs:
        takers = db.q("SELECT user_id FROM job_takers WHERE job_id = ? AND status IN ('taken', 'done')", (j["id"],))
        if any(t["user_id"] == member.id for t in takers):
            continue
        if len(takers) >= j["slots"]:
            continue
        result.append(j)
    return result


def auto_jobs_for(member: discord.Member) -> list[dict]:
    """Автоматические задания: склады, объекты, сборы."""
    now = time.time()
    result = []
    try:
        from cogs import warehouses
        if warehouses.db is not None:
            for w in warehouses.db.all():
                if w["status"] == "closed" or not w["message_id"]:
                    continue
                left = warehouses.expires_at(w) - now
                if 0 < left <= 12 * 3600 and warehouses.can(member, w, "refresh"):
                    result.append({"kind": "auto", "spec": "logist",
                                   "title": f"♻️ Обновить склад №{w['id']} «{w['name']}»",
                                   "details": f"{w['region']}" + (f" / {w['coords']}" if w["coords"] else "")
                                              + f" — удалится <t:{int(warehouses.expires_at(w))}:R>. "
                                              "Обнови в игре и нажми «♻️ Обновил» на карточке.",
                                   "url": warehouses.jump_url(w)})
    except Exception:
        log.exception("Задания: склады")
    try:
        from cogs import industry
        if industry.db is not None and industry.is_worker(member):
            for f in industry.db.fac_active():
                if f["expires_at"] - now <= 3 * 3600:
                    result.append({"kind": "auto", "spec": "engineer",
                                   "title": f"⛽ Обслужить объект «{f['name']}»",
                                   "details": f"{f['place'] or ''} — обслуживание кончается <t:{f['expires_at']}:R>.",
                                   "url": link(f["channel_id"], f["message_id"])})
            for g in industry.db.gat_open():
                if now < g["starts_at"] <= now + 86400 and member.id not in industry.db.gat_people(g["id"], "going"):
                    result.append({"kind": "auto", "spec": "logist",
                                   "title": f"⛏️ Сбор на добычу: {g['what']}",
                                   "details": f"{g['place'] or ''} — <t:{g['starts_at']}:R>. Нажми «Иду» на карточке.",
                                   "url": link(g["channel_id"], g["message_id"])})
    except Exception:
        log.exception("Задания: объекты/сборы")
    return result


def pick_job(member: discord.Member, skip: set[str]) -> Optional[dict]:
    specs = my_specs(member)
    pool = [dict(j, kind="job", url=None) for j in open_jobs_for(member)] + auto_jobs_for(member)
    pool = [j for j in pool if f"{j['kind']}:{j.get('id', j['title'])}" not in skip]
    if not pool:
        return None
    matching = [j for j in pool if j.get("spec") and j["spec"] in specs]
    # офицерские задания — в приоритете, потом подходящие по специализации
    officer = [j for j in pool if j["kind"] == "job" and (not j["spec"] or j["spec"] in specs)]
    for group in (officer, matching, pool):
        if group:
            return random.choice(group)
    return None


def job_embed(j: dict) -> discord.Embed:
    e = discord.Embed(title=f"🎯 {j['title']}", description=j.get("details") or None, color=discord.Color.dark_gold())
    extra = []
    if j.get("spec") and j["spec"] in SPEC:
        emoji, name = SPEC[j["spec"]]
        extra.append(f"{emoji} {name}")
    if j["kind"] == "job":
        taken = db.q("SELECT COUNT(*) AS n FROM job_takers WHERE job_id = ? AND status IN ('taken','done')", (j["id"],))
        extra.append(f"👥 мест: {j['slots'] - taken[0]['n']} из {j['slots']}")
        if j.get("deadline"):
            extra.append(f"⏳ <t:{j['deadline']}:R>")
        extra.append(f"от <@{j['author_id']}>")
    if extra:
        e.add_field(name="​", value=" · ".join(extra), inline=False)
    return e


class JobOfferView(discord.ui.View):
    def __init__(self, member: discord.Member, job: dict, skip: set[str]) -> None:
        super().__init__(timeout=600)
        self.member = member
        self.job = job
        self.skip = skip
        if job["kind"] == "auto":
            self.remove_item(self.take)
            if job.get("url"):
                self.add_item(discord.ui.Button(label="Открыть", emoji="🔗", url=job["url"]))

    @discord.ui.button(label="Беру", emoji="✅", style=discord.ButtonStyle.success)
    async def take(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        j = self.job
        free = [x for x in open_jobs_for(interaction.user) if x["id"] == j["id"]]
        if not free:
            await interaction.response.edit_message(content="Это задание уже разобрали.", embed=None, view=None)
            return
        db.x("INSERT OR REPLACE INTO job_takers (job_id, user_id, status, ts) VALUES (?, ?, 'taken', ?)",
             (j["id"], interaction.user.id, int(time.time())))
        await interaction.response.edit_message(
            content="✅ Задание твоё. Когда выполнишь — «📋 Мои задания» в меню → «Готово».", view=None)
        await dm(interaction.client, j["author_id"], f"🎯 {interaction.user.display_name} взял задание «{j['title']}».")

    @discord.ui.button(label="Другое", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def other(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.skip.add(f"{self.job['kind']}:{self.job.get('id', self.job['title'])}")
        job = pick_job(interaction.user, self.skip)
        if job is None:
            await interaction.response.edit_message(content="Больше заданий сейчас нет. 💪", embed=None, view=None)
            return
        await interaction.response.edit_message(content=None, embed=job_embed(job),
                                                view=JobOfferView(interaction.user, job, self.skip))


async def get_job(interaction: discord.Interaction) -> None:
    if not is_legion(interaction.user):
        await say(interaction, "⛔ Задания — для легионеров.")
        return
    job = pick_job(interaction.user, set())
    if job is None:
        await say(interaction, "Сейчас заданий нет — всё под контролем. 💪")
        return
    await say(interaction, embed=job_embed(job), view=JobOfferView(interaction.user, job, set()))


class MyJobsView(discord.ui.View):
    def __init__(self, jobs: list[dict]) -> None:
        super().__init__(timeout=600)
        if jobs:
            self.pick.options = [discord.SelectOption(label=j["title"][:100], value=str(j["id"]),
                                                      description=(j["details"] or "")[:100] or None)
                                 for j in jobs[:25]]
        else:
            self.remove_item(self.pick)
            self.remove_item(self.drop)
        self.choice: Optional[int] = None

    @discord.ui.select(placeholder="Выберите задание…", options=[discord.SelectOption(label="—")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.choice = int(select.values[0])
        db.x("UPDATE job_takers SET status = 'done', done_ts = ? WHERE job_id = ? AND user_id = ?",
             (int(time.time()), self.choice, interaction.user.id))
        job = db.q("SELECT * FROM jobs WHERE id = ?", (self.choice,))[0]
        done = db.q("SELECT COUNT(*) AS n FROM job_takers WHERE job_id = ? AND status = 'done'", (job["id"],))[0]["n"]
        if done >= job["slots"]:
            db.x("UPDATE jobs SET status = 'closed' WHERE id = ?", (job["id"],))
        await interaction.response.edit_message(content=f"🏁 Задание «{job['title']}» выполнено. Ave!", view=None,
                                                embed=None)
        await dm(interaction.client, job["author_id"],
                 f"🏁 {interaction.user.display_name} выполнил задание «{job['title']}».")

    @discord.ui.button(label="Отказаться от задания", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def drop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        jobs = my_jobs(interaction.user.id)
        view = discord.ui.View(timeout=300)
        sel = discord.ui.Select(placeholder="От какого отказаться?",
                                options=[discord.SelectOption(label=j["title"][:100], value=str(j["id"])) for j in jobs[:25]])

        async def cb(inter: discord.Interaction) -> None:
            db.x("UPDATE job_takers SET status = 'dropped' WHERE job_id = ? AND user_id = ?",
                 (int(sel.values[0]), inter.user.id))
            await inter.response.edit_message(content="↩️ Вы отказались от задания.", view=None)
        sel.callback = cb
        view.add_item(sel)
        await interaction.response.edit_message(content="Отказаться:", embed=None, view=view)


def my_jobs(user_id: int) -> list[dict]:
    return db.q("SELECT j.* FROM jobs j JOIN job_takers t ON t.job_id = j.id "
                "WHERE t.user_id = ? AND t.status = 'taken' ORDER BY j.id", (user_id,))


def jobs_done(user_id: int) -> int:
    return db.q("SELECT COUNT(*) AS n FROM job_takers WHERE user_id = ? AND status = 'done'", (user_id,))[0]["n"]


class JobForm(discord.ui.Modal, title="Открытое задание для легионеров"):
    jtitle = discord.ui.TextInput(label="Что сделать (коротко)", max_length=100,
                                  placeholder="Например: отвезти 50 Бматов в Deadlands")
    details = discord.ui.TextInput(label="Подробности (необязательно)", style=discord.TextStyle.paragraph,
                                   max_length=800, required=False)
    slots = discord.ui.TextInput(label="Сколько человек нужно", max_length=2, default="1")
    deadline = discord.ui.TextInput(label="Актуально до (необязательно)", max_length=30, required=False,
                                    placeholder="30.11 · 23.11 21:00 · через 6ч")

    def __init__(self, spec: str) -> None:
        super().__init__()
        self.spec = spec

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await say(interaction, "⛔ Задания выкладывают офицеры.")
            return
        try:
            slots = max(1, min(int(self.slots.value), 50))
        except ValueError:
            slots = 1
        deadline = None
        if self.deadline.value.strip():
            from cogs.operations import parse_start
            from cogs.profiles import parse_until
            deadline = parse_start(self.deadline.value) or parse_until(self.deadline.value)
            if deadline is None:
                await say(interaction, "Не понял срок. Примеры: `30.11`, `23.11 21:00`, `через 6ч`.")
                return
        jid = db.x("INSERT INTO jobs (title, details, spec, slots, deadline, author_id, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (self.jtitle.value.strip(), self.details.value.strip(), self.spec, slots, deadline,
                    interaction.user.id, int(time.time())))
        await say(interaction, f"✅ Задание №{jid} выложено. Легионеры получат его через «🎯 Получить задание».")


class JobSpecView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.spec.options = [discord.SelectOption(label="Любой легионер", value="any", emoji="🦅")] + [
            discord.SelectOption(label=n, value=k, emoji=e, description=SPEC_HINTS[k]) for k, e, n in SPECS]

    @discord.ui.select(placeholder="Для кого задание?", options=[discord.SelectOption(label="—")])
    async def spec(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        value = select.values[0]
        await interaction.response.send_modal(JobForm("" if value == "any" else value))


# ================= 🎯 СПЕЦИАЛИЗАЦИИ =================

def spec_roles() -> dict[str, int]:
    result = {}
    for key, _, _ in SPECS:
        v = db.meta(f"spec:{key}")
        if v:
            result[key] = int(v)
    return result


async def ensure_spec_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    roles = {}
    known = spec_roles()
    for key, emoji, name in SPECS:
        role = guild.get_role(known[key]) if key in known else None
        if role is None:
            role = discord.utils.get(guild.roles, name=name)
        if role is None and key in OLD_SPEC_NAMES:
            role = discord.utils.get(guild.roles, name=OLD_SPEC_NAMES[key])
        if role is None:
            role = await guild.create_role(name=name, mentionable=True, reason="Специализации Легиона")
        elif role.name != name:
            await role.edit(name=name, reason="Римские названия специализаций")
        db.set_meta(f"spec:{key}", str(role.id))
        roles[key] = role
    return roles


class SpecView(discord.ui.View):
    def __init__(self, member: discord.Member) -> None:
        super().__init__(timeout=300)
        mine = my_specs(member)
        self.pick.options = [discord.SelectOption(label=n, value=k, emoji=e, description=SPEC_HINTS[k],
                                                  default=k in mine) for k, e, n in SPECS]
        self.pick.max_values = 3

    @discord.ui.select(placeholder="До трёх специализаций", min_values=0, options=[discord.SelectOption(label="—")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        member = interaction.user
        try:
            roles = await ensure_spec_roles(interaction.guild)
        except discord.HTTPException:
            await interaction.response.edit_message(
                content="⚠️ Не удалось создать роли: дайте боту право «Управлять ролями».", view=None)
            return
        chosen = set(select.values)
        add = [roles[k] for k in chosen if roles[k] not in member.roles]
        remove = [roles[k] for k in roles if k not in chosen and roles[k] in member.roles]
        try:
            if add:
                await member.add_roles(*add, reason="Специализация")
            if remove:
                await member.remove_roles(*remove, reason="Специализация")
        except discord.HTTPException:
            await interaction.response.edit_message(
                content="⚠️ Не хватает прав: роль бота должна быть выше ролей специализаций.", view=None)
            return
        names = ", ".join(f"{SPEC[k][0]} {SPEC[k][1]}" for k in chosen) or "—"
        try:
            from cogs import profiles
            if profiles.db is not None:
                profiles.db.save(member.id, specialty=", ".join(SPEC[k][1] for k in chosen))
        except Exception:
            pass
        await interaction.response.edit_message(content=f"🎯 Специализации: {names}", view=None)


# ================= 📈 ПУТЬ ЛЕГИОНЕРА =================

def rank_steps() -> list[tuple[str, int]]:
    return [(k, rid) for k, rid in (("fabrum", config.FABRUM_ROLE_ID), ("centurion", config.CENTURION_ROLE_ID),
                                    ("legate", config.LEGATE_ROLE_ID)) if rid]


def path_embed(member: discord.Member) -> discord.Embed:
    held = {r.id for r in member.roles}
    steps = rank_steps()
    e = discord.Embed(title=f"📈 Путь легионера · {member.display_name}", color=discord.Color.dark_gold())
    target = None
    for key, rid in steps:
        if rid not in held:
            target = (key, rid)
            break
    higher = {config.CONSUL_ROLE_ID, config.SENATE_ROLE_ID, config.PRINCEPS_ROLE_ID} - {0}
    if target is None or held & higher:
        e.description = "Дальше — только выборами: Консулов и Сенат выбирает Легион. 🏛️"
        return e
    key, rid = target
    req = db.q("SELECT * FROM path WHERE rank = ?", (key,))[0]
    try:
        from cogs import profiles
        app = profiles.application(member.id)
        since = profiles.iso_ts(app.get("reviewed_at")) if app.get("status") == "approved" else None
        stats = profiles.gather(member.id)
    except Exception:
        since, stats = None, {}
    start = since or (int(member.joined_at.timestamp()) if member.joined_at else int(time.time()))
    days = int((time.time() - start) / 86400)
    ops = stats.get("ops_came", 0)
    jobs = jobs_done(member.id) + stats.get("orders_done", 0) + stats.get("tasks_done", 0)

    def bar(have: int, need: int) -> str:
        if need <= 0:
            return "✅"
        pct = min(have / need, 1)
        return ("✅ " if have >= need else "") + "█" * round(pct * 8) + "░" * (8 - round(pct * 8)) + f" {have}/{need}"

    e.description = (f"Следующее звание: <@&{rid}>\n\n"
                     f"📅 Дней в Легионе: {bar(days, req['days'])}\n"
                     f"⚔️ Операций (явка): {bar(ops, req['ops'])}\n"
                     f"🎯 Заданий и заказов: {bar(jobs, req['jobs'])}")
    if days >= req["days"] and ops >= req["ops"] and jobs >= req["jobs"]:
        e.description += "\n\n🎖️ Все условия выполнены — офицеры могут представить тебя к званию."
    e.set_footer(text="Повышает Сенат по представлению; условия — ориентир.")
    return e


# ================= 📨 РАПОРТЫ =================

def report_embed(r: dict) -> discord.Embed:
    emoji, kind = REPORT_KINDS[r["kind"]]
    who = "аноним" if r["anonymous"] else f"<@{r['author_id']}>"
    lines = [r["text"][:1500], "", f"{REPORT_STATUS[r['status']]} · от {who} · <t:{r['ts']}:R>"]
    if r["officer_id"]:
        lines.append(f"🙋 <@{r['officer_id']}>")
    if r["answer"]:
        lines.append(f"💬 {r['answer'][:800]}")
    color = {"new": discord.Color.blurple(), "taken": discord.Color.gold(), "closed": discord.Color.dark_grey()}
    return discord.Embed(title=f"📨 Рапорт №{r['id']} · {emoji} {kind}", description="\n".join(lines)[:4000],
                         color=color[r["status"]])


class ReportButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rep:(?P<rid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, rid: int, act: str, label: Optional[str] = None, emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"rep:{rid}:{act}"))
        self.rid = rid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["rid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await say(interaction, "⛔ Рапорты рассматривают офицеры.")
            return
        rows = db.q("SELECT * FROM reports WHERE id = ?", (self.rid,))
        if not rows or rows[0]["status"] == "closed":
            await say(interaction, "Рапорт уже закрыт.")
            return
        r = rows[0]
        if self.act == "take":
            db.x("UPDATE reports SET status = 'taken', officer_id = ? WHERE id = ?", (interaction.user.id, r["id"]))
            r = db.q("SELECT * FROM reports WHERE id = ?", (r["id"],))[0]
            await interaction.response.edit_message(embed=report_embed(r), view=report_view(r))
            await dm(interaction.client, r["author_id"],
                     f"🙋 Ваш рапорт №{r['id']} взял в работу {interaction.user.display_name}.")
        elif self.act == "answer":
            await interaction.response.send_modal(AnswerModal(r["id"], interaction.message))


def report_view(r: dict) -> Optional[discord.ui.View]:
    if r["status"] == "closed":
        return None
    view = discord.ui.View(timeout=None)
    if r["status"] == "new":
        view.add_item(ReportButton(r["id"], "take", "Взять", "🙋", discord.ButtonStyle.primary))
    view.add_item(ReportButton(r["id"], "answer", "Ответить и закрыть", "💬", discord.ButtonStyle.success))
    return view


class AnswerModal(discord.ui.Modal, title="Ответ на рапорт"):
    answer = discord.ui.TextInput(label="Ответ (придёт автору в личку)", style=discord.TextStyle.paragraph,
                                  max_length=1000)

    def __init__(self, rid: int, message: discord.Message) -> None:
        super().__init__()
        self.rid = rid
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db.x("UPDATE reports SET status = 'closed', answer = ?, officer_id = ? WHERE id = ?",
             (self.answer.value.strip(), interaction.user.id, self.rid))
        r = db.q("SELECT * FROM reports WHERE id = ?", (self.rid,))[0]
        await interaction.response.edit_message(embed=report_embed(r), view=None)
        await dm(interaction.client, r["author_id"],
                 f"📨 **Ответ на ваш рапорт №{r['id']}** от {interaction.user.display_name}:\n{r['answer']}")


class ReportForm(discord.ui.Modal):
    def __init__(self, kind: str, anonymous: bool) -> None:
        emoji, name = REPORT_KINDS[kind]
        super().__init__(title=f"Рапорт: {name}" + (" (анонимно)" if anonymous else ""))
        self.kind = kind
        self.anonymous = anonymous
        self.text = discord.ui.TextInput(label="Текст рапорта", style=discord.TextStyle.paragraph, max_length=1500)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = interaction.client.get_channel(config.SENATE_CHANNEL_ID) if config.SENATE_CHANNEL_ID else None
        if channel is None:
            await say(interaction, "⚠️ Канал Сената не настроен — сообщите командованию.")
            return
        rid = db.x("INSERT INTO reports (author_id, kind, anonymous, text, ts) VALUES (?, ?, ?, ?, ?)",
                   (interaction.user.id, self.kind, int(self.anonymous), self.text.value.strip(), int(time.time())))
        r = db.q("SELECT * FROM reports WHERE id = ?", (rid,))[0]
        try:
            msg = await channel.send(embed=report_embed(r), view=report_view(r),
                                     allowed_mentions=discord.AllowedMentions.none())
            db.x("UPDATE reports SET channel_id = ?, message_id = ? WHERE id = ?", (msg.channel.id, msg.id, rid))
        except discord.HTTPException:
            await say(interaction, "⚠️ Не удалось доставить рапорт. Сообщите командованию.")
            return
        await say(interaction, f"📨 Рапорт №{rid} отправлен командованию. Ответ придёт в личку.")


class ReportKindView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.anonymous = False
        self.kind.options = [discord.SelectOption(label=n, value=k, emoji=e) for k, (e, n) in REPORT_KINDS.items()]

    @discord.ui.button(label="Анонимно: нет", emoji="🕶️", style=discord.ButtonStyle.secondary, row=1)
    async def anon(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.anonymous = not self.anonymous
        button.label = f"Анонимно: {'да' if self.anonymous else 'нет'}"
        button.style = discord.ButtonStyle.primary if self.anonymous else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.select(placeholder="О чём рапорт?", options=[discord.SelectOption(label="—")], row=0)
    async def kind(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        await interaction.response.send_modal(ReportForm(select.values[0], self.anonymous))


# ================= 💡 ПРЕДЛОЖЕНИЯ =================

def proposal_embed(p: dict) -> discord.Embed:
    label, color = PROPOSAL_STATUS[p["status"]]
    rows = db.q("SELECT vote, COUNT(*) AS n FROM proposal_votes WHERE proposal_id = ? GROUP BY vote", (p["id"],))
    counts = {r["vote"]: r["n"] for r in rows}
    lines = []
    if p["text"]:
        lines += [p["text"][:1500], ""]
    lines.append(f"👍 **{counts.get(1, 0)}** · 👎 **{counts.get(0, 0)}** · {label} · 💡 <@{p['author_id']}>")
    if p["comment"]:
        lines.append(f"🏛️ <@{p['decided_by']}>: {p['comment'][:500]}")
    return discord.Embed(title=f"💡 {p['title']}", description="\n".join(lines)[:4000], color=color)


class ProposalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"prop:(?P<pid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, pid: int, act: str, label: Optional[str] = None, emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"prop:{pid}:{act}"))
        self.pid = pid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["pid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        rows = db.q("SELECT * FROM proposals WHERE id = ?", (self.pid,))
        if not rows:
            return
        p = rows[0]
        if self.act in ("yes", "no"):
            if p["status"] != "open":
                await say(interaction, "Голосование по предложению закрыто.")
                return
            if not is_legion(interaction.user):
                await say(interaction, "⛔ Голосуют легионеры.")
                return
            db.x("INSERT OR REPLACE INTO proposal_votes VALUES (?, ?, ?)",
                 (p["id"], interaction.user.id, 1 if self.act == "yes" else 0))
            await interaction.response.edit_message(embed=proposal_embed(p))
        elif self.act == "decide":
            if not is_officer(interaction.user):
                await say(interaction, "⛔ Решение принимают офицеры.")
                return
            await say(interaction, "Решение по предложению:", view=DecideView(p["id"], interaction.message))


def proposal_view(p: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if p["status"] == "open":
        view.add_item(ProposalButton(p["id"], "yes", None, "👍", discord.ButtonStyle.success))
        view.add_item(ProposalButton(p["id"], "no", None, "👎", discord.ButtonStyle.secondary))
    view.add_item(ProposalButton(p["id"], "decide", None, "⚖️", discord.ButtonStyle.secondary))
    return view


class DecideModal(discord.ui.Modal, title="Решение по предложению"):
    comment = discord.ui.TextInput(label="Комментарий (необязательно)", style=discord.TextStyle.paragraph,
                                   max_length=500, required=False)

    def __init__(self, pid: int, status: str, message: discord.Message) -> None:
        super().__init__()
        self.pid = pid
        self.status = status
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db.x("UPDATE proposals SET status = ?, comment = ?, decided_by = ? WHERE id = ?",
             (self.status, self.comment.value.strip(), interaction.user.id, self.pid))
        p = db.q("SELECT * FROM proposals WHERE id = ?", (self.pid,))[0]
        await interaction.response.edit_message(content=f"✅ {PROPOSAL_STATUS[self.status][0]}", view=None)
        try:
            await self.message.edit(embed=proposal_embed(p), view=proposal_view(p))
        except discord.HTTPException:
            pass
        await dm(interaction.client, p["author_id"],
                 f"💡 Ваше предложение «{p['title']}»: {PROPOSAL_STATUS[self.status][0]}"
                 + (f"\n{p['comment']}" if p["comment"] else ""))


class DecideView(discord.ui.View):
    def __init__(self, pid: int, message: discord.Message) -> None:
        super().__init__(timeout=300)
        self.pid = pid
        self.message = message
        self.pick.options = [discord.SelectOption(label=label, value=key)
                             for key, (label, _) in PROPOSAL_STATUS.items()]

    @discord.ui.select(placeholder="Статус", options=[discord.SelectOption(label="—")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        await interaction.response.send_modal(DecideModal(self.pid, select.values[0], self.message))


class ProposalForm(discord.ui.Modal, title="Предложение Легиону"):
    ptitle = discord.ui.TextInput(label="Суть (коротко)", max_length=100)
    text = discord.ui.TextInput(label="Подробнее (необязательно)", style=discord.TextStyle.paragraph,
                                max_length=1500, required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_legion(interaction.user):
            await say(interaction, "⛔ Предложения — для легионеров.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pid = db.x("INSERT INTO proposals (author_id, title, text, ts) VALUES (?, ?, ?, ?)",
                   (interaction.user.id, self.ptitle.value.strip(), self.text.value.strip(), int(time.time())))
        p = db.q("SELECT * FROM proposals WHERE id = ?", (pid,))[0]
        try:
            msg = await interaction.channel.send(embed=proposal_embed(p), view=proposal_view(p),
                                                 allowed_mentions=discord.AllowedMentions.none())
            db.x("UPDATE proposals SET channel_id = ?, message_id = ? WHERE id = ?", (msg.channel.id, msg.id, pid))
        except discord.HTTPException as e:
            await interaction.followup.send(f"Не удалось опубликовать: {e.text or e}", ephemeral=True)
            return
        await interaction.followup.send("💡 Предложение опубликовано — легионеры проголосуют, офицеры решат.",
                                        ephemeral=True)


# ================= 🏖️ БРЕЙКВАР =================

class BreakwarModal(discord.ui.Modal, title="Брейквар"):
    until = discord.ui.TextInput(label="До какого числа (или срок)", max_length=20,
                                 placeholder="30.11 · 7д · 2 недели")
    reason = discord.ui.TextInput(label="Причина (необязательно)", max_length=200, required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs import profiles
        until = profiles.parse_until(self.until.value)
        if until is None:
            await say(interaction, "Не понял дату. Примеры: `30.11`, `7д`, `2 недели` (до 120 дней).")
            return
        profiles.db.set_vacation(interaction.user.id, until, self.reason.value.strip())
        await say(interaction, f"🏖️ Брейквар до <t:{until}:D>. Возвращайся! 🦅")


class BreakwarView(discord.ui.View):
    def __init__(self, on_break: bool) -> None:
        super().__init__(timeout=300)
        if on_break:
            self.remove_item(self.start)
        else:
            self.remove_item(self.back)

    @discord.ui.button(label="Уйти в брейквар", emoji="🏖️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BreakwarModal())

    @discord.ui.button(label="Вернулся", emoji="⚔️", style=discord.ButtonStyle.success)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs import profiles
        profiles.db.end_vacation(interaction.user.id)
        await interaction.response.edit_message(content="⚔️ С возвращением в строй!", view=None)


# ================= ПАНЕЛЬ =================

MENU = [
    ("profile", "Мой профиль", "🪪"),
    ("spec", "Специализация", "🎯"),
    ("path", "Путь легионера", "📈"),
    ("myjobs", "Мои задания", "📋"),
    ("report", "Связь с командованием", "📨"),
    ("proposal", "Предложение Легиону", "💡"),
    ("breakwar", "Брейквар", "🏖️"),
    ("help", "Помощь", "❓"),
]


class LegionPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.menu.options = [discord.SelectOption(label=label, value=key, emoji=emoji) for key, label, emoji in MENU]

    @discord.ui.button(label="Актуальное", emoji="📢", style=discord.ButtonStyle.primary, custom_id="leg:actual")
    async def actual(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await say(interaction, embed=actual_embed(interaction.user))

    @discord.ui.button(label="Получить задание", emoji="🎯", style=discord.ButtonStyle.success, custom_id="leg:job")
    async def job(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await get_job(interaction)

    @discord.ui.select(placeholder="🦅 Легион…", custom_id="leg:menu", options=[discord.SelectOption(label="—")])
    async def menu(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        choice = select.values[0]
        user = interaction.user
        if choice == "profile":
            from cogs.profiles import show_profile
            await show_profile(interaction, user)
        elif choice == "spec":
            await say(interaction, "🎯 Выберите специализации (до трёх):", view=SpecView(user))
        elif choice == "path":
            await say(interaction, embed=path_embed(user))
        elif choice == "myjobs":
            jobs = my_jobs(user.id)
            text = ("📋 Ваши задания — выберите выполненное:" if jobs
                    else "📋 Заданий нет. Возьмите через «🎯 Получить задание».")
            if jobs:
                await say(interaction, text, view=MyJobsView(jobs))
            else:
                await say(interaction, text)
        elif choice == "report":
            await say(interaction, "📨 Рапорт командованию. Ответ придёт в личку.", view=ReportKindView())
        elif choice == "proposal":
            await interaction.response.send_modal(ProposalForm())
        elif choice == "breakwar":
            from cogs import profiles
            vac = profiles.db.vacation(user.id)
            text = (f"🏖️ Вы в брейкваре до <t:{vac['until']}:D>." if vac else "🏖️ Брейквар — отметить отсутствие.")
            await say(interaction, text, view=BreakwarView(bool(vac)))
        elif choice == "help":
            from cogs.help import build_help
            await say(interaction, embed=build_help(user))


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🦅 ЛЕГИОН",
        description="📢 что происходит · 🎯 чем помочь Легиону · остальное — в меню.",
        color=discord.Color.dark_red(),
    )


class Legion(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="легион", description="Опубликовать и закрепить панель Легиона в этом канале")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await say(interaction, "⛔ Недостаточно прав.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await interaction.channel.send(embed=panel_embed(), view=LegionPanel())
        except discord.HTTPException as e:
            await interaction.followup.send(f"Не удалось опубликовать панель: {e.text or e}", ephemeral=True)
            return
        try:
            await message.pin(reason="Панель Легиона")
            note = "и закреплена"
        except discord.HTTPException:
            note = "но закрепить не вышло — закрепите вручную"
        await interaction.followup.send(f"✅ Панель опубликована {note}.", ephemeral=True)

    @app_commands.command(name="задание", description="Выложить открытое задание для легионеров (офицеры)")
    @app_commands.guild_only()
    async def new_job(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await say(interaction, "⛔ Задания выкладывают офицеры.")
            return
        await say(interaction, "🎯 Для кого задание?", view=JobSpecView())

    @app_commands.command(name="путь_настроить", description="Условия для звания в «Пути легионера» (офицеры)")
    @app_commands.describe(звание="Для какого звания", дней="Дней в Легионе", операций="Операций с явкой",
                           заданий="Заданий, заказов и поручений")
    @app_commands.choices(звание=[app_commands.Choice(name="Фабрум", value="fabrum"),
                                  app_commands.Choice(name="Центурион", value="centurion"),
                                  app_commands.Choice(name="Легат", value="legate")])
    @app_commands.guild_only()
    async def set_path(self, interaction: discord.Interaction, звание: app_commands.Choice[str], дней: int,
                       операций: int, заданий: int) -> None:
        from cogs.consulate import can_create
        if not can_create(interaction.user):
            await say(interaction, "⛔ Условия меняют Принцепс, Консулы и Сенат.")
            return
        db.x("INSERT OR REPLACE INTO path VALUES (?, ?, ?, ?)", (звание.value, max(дней, 0), max(операций, 0),
                                                                 max(заданий, 0)))
        await say(interaction, f"✅ {звание.name}: {дней} дн., {операций} операций, {заданий} заданий.")


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_view(LegionPanel())
    bot.add_dynamic_items(ReportButton, ProposalButton)
    await bot.add_cog(Legion(bot))
