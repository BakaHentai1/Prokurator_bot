"""Профиль легионера.

/профиль [участник] или долгое нажатие на человека → Приложения → «Профиль».

В профиле: звание и история званий, ник, специализация, часовой пояс, даты службы,
статус (отпуск), явка на операции, логистика, вклад в Консулат, выборы, награды,
достижения, благодарности, голосовая активность, склады.

Команды:
  /наградить, /награда_снять — награды (Принцепс, Консулы, Сенат, Легаты)
  /спасибо — благодарность легионеру (не чаще раза в сутки)
  /отпуск, /вернулся, /отпуска — отсутствие
Данные — в отдельном файле data/profiles.db.
"""
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("profiles")

DB_PATH: Path = config.DATA_DIR / "profiles.db"

AWARDS = {
    "valor": ("🏅", "Фалера за доблесть"),
    "civic": ("🌿", "Гражданский венок — спас товарищей"),
    "mural": ("🧱", "Стенной венок — первым на штурме"),
    "naval": ("⚓", "Морской венок — за флот"),
    "logist": ("🚚", "Фалера логиста"),
    "faber": ("🔧", "Фалера фабра — за производство"),
    "scout": ("🔭", "Фалера разведчика"),
    "gold": ("👑", "Золотой венок — особые заслуги"),
}


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id    INTEGER PRIMARY KEY,
                nickname   TEXT NOT NULL DEFAULT '',
                specialty  TEXT NOT NULL DEFAULT '',
                timezone   TEXT NOT NULL DEFAULT '',
                about      TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS role_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                role_id  INTEGER NOT NULL,
                action   TEXT    NOT NULL,      -- add / remove
                by_id    INTEGER,
                ts       INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rh_user ON role_history (user_id);
            CREATE TABLE IF NOT EXISTS awards (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                award    TEXT    NOT NULL,
                reason   TEXT    NOT NULL DEFAULT '',
                by_id    INTEGER NOT NULL,
                ts       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thanks (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id  INTEGER NOT NULL,
                to_id    INTEGER NOT NULL,
                reason   TEXT    NOT NULL DEFAULT '',
                ts       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice (
                user_id  INTEGER NOT NULL,
                day      TEXT    NOT NULL,      -- YYYY-MM-DD
                seconds  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
            CREATE TABLE IF NOT EXISTS vacations (
                user_id  INTEGER PRIMARY KEY,
                until    INTEGER NOT NULL,
                reason   TEXT    NOT NULL DEFAULT '',
                ts       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                user_id      INTEGER PRIMARY KEY,
                last_message INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('tracking_since', ?)", (str(int(time.time())),))
        self.conn.commit()

    # активность (только время последнего сообщения, без текста)
    def touch_messages(self, stamps: dict[int, int]) -> None:
        for uid, ts in stamps.items():
            self.conn.execute(
                "INSERT INTO activity (user_id, last_message) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_message = MAX(last_message, excluded.last_message)",
                (uid, ts))
        self.conn.commit()

    def tracking_since(self) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'tracking_since'").fetchone()
        return int(row[0]) if row else int(time.time())

    # профиль
    def get(self, user_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else {}

    def save(self, user_id: int, **fields) -> None:
        current = self.get(user_id)
        data = {k: fields.get(k, current.get(k, "")) for k in ("nickname", "specialty", "timezone", "about")}
        self.conn.execute(
            "INSERT OR REPLACE INTO profiles (user_id, nickname, specialty, timezone, about, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, data["nickname"], data["specialty"], data["timezone"], data["about"], int(time.time())),
        )
        self.conn.commit()

    # звания
    def add_role_event(self, user_id: int, role_id: int, action: str, by_id: Optional[int]) -> None:
        self.conn.execute("INSERT INTO role_history (user_id, role_id, action, by_id, ts) VALUES (?, ?, ?, ?, ?)",
                          (user_id, role_id, action, by_id, int(time.time())))
        self.conn.commit()

    def role_history(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM role_history WHERE user_id = ? ORDER BY ts", (user_id,))]

    # награды
    def add_award(self, user_id: int, award: str, reason: str, by_id: int) -> int:
        cur = self.conn.execute("INSERT INTO awards (user_id, award, reason, by_id, ts) VALUES (?, ?, ?, ?, ?)",
                                (user_id, award, reason, by_id, int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def awards(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM awards WHERE user_id = ? ORDER BY ts DESC", (user_id,))]

    def get_award(self, aid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM awards WHERE id = ?", (aid,)).fetchone()
        return dict(row) if row else None

    def remove_award(self, aid: int) -> None:
        self.conn.execute("DELETE FROM awards WHERE id = ?", (aid,))
        self.conn.commit()

    # благодарности
    def last_thanks_from(self, from_id: int) -> Optional[int]:
        row = self.conn.execute("SELECT MAX(ts) FROM thanks WHERE from_id = ?", (from_id,)).fetchone()
        return row[0]

    def add_thanks(self, from_id: int, to_id: int, reason: str) -> None:
        self.conn.execute("INSERT INTO thanks (from_id, to_id, reason, ts) VALUES (?, ?, ?, ?)",
                          (from_id, to_id, reason, int(time.time())))
        self.conn.commit()

    def thanks_to(self, user_id: int) -> tuple[int, list[dict]]:
        total = self.conn.execute("SELECT COUNT(*) FROM thanks WHERE to_id = ?", (user_id,)).fetchone()[0]
        recent = [dict(r) for r in self.conn.execute(
            "SELECT * FROM thanks WHERE to_id = ? ORDER BY ts DESC LIMIT 3", (user_id,))]
        return total, recent

    # голос
    def add_voice(self, user_id: int, start: float, end: float) -> None:
        while start < end:
            day_start = datetime.fromtimestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
            next_day = (day_start + timedelta(days=1)).timestamp()
            chunk_end = min(end, next_day)
            seconds = int(chunk_end - start)
            if seconds > 0:
                self.conn.execute(
                    "INSERT INTO voice (user_id, day, seconds) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
                    (user_id, day_start.strftime("%Y-%m-%d"), seconds))
            start = chunk_end
        self.conn.commit()

    def voice_hours(self, user_id: int, days: int) -> float:
        since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        row = self.conn.execute("SELECT COALESCE(SUM(seconds), 0) FROM voice WHERE user_id = ? AND day >= ?",
                                (user_id, since)).fetchone()
        return row[0] / 3600

    # отпуск
    def set_vacation(self, user_id: int, until: int, reason: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO vacations VALUES (?, ?, ?, ?)",
                          (user_id, until, reason, int(time.time())))
        self.conn.commit()

    def end_vacation(self, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM vacations WHERE user_id = ?", (user_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def vacation(self, user_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM vacations WHERE user_id = ? AND until > ?",
                                (user_id, int(time.time()))).fetchone()
        return dict(row) if row else None

    def vacations(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM vacations WHERE until > ? ORDER BY until", (int(time.time()),))]


db: Optional[DB] = None


# ---------- права ----------

def can_award(member: discord.abc.User) -> bool:
    from cogs.operations import can_create_op  # Принцепс, Консулы, Сенат, Легаты
    return can_create_op(member)


def is_officer(member: discord.abc.User) -> bool:
    from cogs.consulate import has_any, is_admin, officer_roles
    return is_admin(member) or has_any(member, officer_roles())


# ---------- звания ----------

def rank_order() -> list[int]:
    return [r for r in (
        config.PRINCEPS_ROLE_ID, config.SENATE_ROLE_ID, config.CONSUL_ROLE_ID, config.LEGATE_ROLE_ID,
        config.CENTURION_ROLE_ID, config.FABRUM_ROLE_ID, config.MEMBER_ROLE_ID, config.ALLY_ROLE_ID,
        config.WARDEN_ROLE_ID, config.NEWCOMER_ROLE_ID,
    ) if r]


def rank_of(member: discord.Member) -> str:
    held = {r.id for r in member.roles}
    for role_id in rank_order():
        if role_id in held:
            return f"<@&{role_id}>"
    return member.top_role.mention if not member.top_role.is_default() else "—"


def rank_path(user_id: int) -> Optional[str]:
    ranks = set(rank_order())
    steps = [h for h in db.role_history(user_id) if h["role_id"] in ranks and h["action"] == "add"]
    if not steps:
        return None
    return " → ".join(f"<@&{h['role_id']}> <t:{h['ts']}:d>" for h in steps[-6:])


# ---------- данные из других модулей ----------

def application(user_id: int) -> dict:
    try:
        from cogs.registration import load_data
        return load_data().get(str(user_id), {})
    except Exception:
        return {}


def iso_ts(value) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return None


def gather(user_id: int) -> dict:
    """Все цифры для профиля и достижений."""
    s: dict = {}
    try:
        from cogs import operations
        if operations.db is not None:
            c = operations.db.conn
            rows = c.execute(
                "SELECT r.answer, COUNT(*) AS n FROM responses r JOIN operations o ON o.id = r.op_id "
                "WHERE r.user_id = ? AND o.status != 'cancelled' GROUP BY r.answer", (user_id,)).fetchall()
            s["ops_answers"] = {r["answer"]: r["n"] for r in rows}
            s["ops_led"] = c.execute(
                "SELECT COUNT(*) FROM operations WHERE creator_id = ? AND status != 'cancelled'",
                (user_id,)).fetchone()[0]
            s["ops_came"] = c.execute("SELECT COUNT(*) FROM attendance WHERE user_id = ?", (user_id,)).fetchone()[0]
            s["ops_marked_signed"] = c.execute(
                "SELECT COUNT(*) FROM responses r WHERE r.user_id = ? AND r.answer IN ('going', 'late') "
                "AND EXISTS (SELECT 1 FROM attendance a WHERE a.op_id = r.op_id)", (user_id,)).fetchone()[0]
    except Exception:
        log.exception("Профиль: операции")
    try:
        from cogs import consulate
        if consulate.db is not None:
            c = consulate.db.conn
            s["votes"] = c.execute("SELECT COUNT(DISTINCT poll_id) FROM votes WHERE user_id = ?",
                                   (user_id,)).fetchone()[0]
            s["polls_made"] = c.execute("SELECT COUNT(*) FROM polls WHERE creator_id = ? AND message_id IS NOT NULL",
                                        (user_id,)).fetchone()[0]
    except Exception:
        log.exception("Профиль: голосования")
    try:
        from cogs import edicts
        if edicts.db is not None:
            c = edicts.db.conn
            s["acks"] = c.execute("SELECT COUNT(*) FROM acks WHERE user_id = ?", (user_id,)).fetchone()[0]
            s["edicts_made"] = c.execute("SELECT COUNT(*) FROM edicts WHERE author_id = ? AND status = 'published'",
                                         (user_id,)).fetchone()[0]
    except Exception:
        log.exception("Профиль: эдикты")
    try:
        from cogs import elections
        if elections.db is not None:
            c = elections.db.conn
            cand = c.execute("SELECT election_id FROM candidates WHERE user_id = ?", (user_id,)).fetchall()
            won = []
            for r in cand:
                e = elections.db.get(r["election_id"])
                if e and e["phase"] == "done" and user_id in elections.results(e)[1]:
                    won.append(e["position"])
            s["elect_ran"] = len(cand)
            s["elect_won"] = won
            s["elect_voted"] = c.execute("SELECT COUNT(DISTINCT election_id) FROM ballots WHERE voter_id = ?",
                                         (user_id,)).fetchone()[0]
            s["elect_made"] = c.execute("SELECT COUNT(*) FROM elections WHERE creator_id = ? AND message_id IS NOT NULL",
                                        (user_id,)).fetchone()[0]
    except Exception:
        log.exception("Профиль: выборы")
    try:
        from cogs import warehouses
        if warehouses.db is not None:
            c = warehouses.db.conn
            s["wh_created"] = c.execute("SELECT COUNT(*) FROM warehouses WHERE created_by = ?",
                                        (user_id,)).fetchone()[0]
            s["wh_refreshes"] = c.execute("SELECT COUNT(*) FROM audit_log WHERE user_id = ? AND action = 'refresh'",
                                          (user_id,)).fetchone()[0]
    except Exception:
        log.exception("Профиль: склады")
    try:
        from cogs import tasks as task_mod
        if task_mod.db is not None:
            s["tasks_done"] = task_mod.db.done_count(user_id)
    except Exception:
        log.exception("Профиль: поручения")
    s["thanks"], s["thanks_recent"] = db.thanks_to(user_id)
    s["awards"] = db.awards(user_id)
    s["voice_week"] = db.voice_hours(user_id, 7)
    s["voice_month"] = db.voice_hours(user_id, 30)
    return s


def warehouses_text(user_id: int, viewer: discord.abc.User) -> Optional[str]:
    try:
        from cogs import warehouses
        if warehouses.db is None:
            return None
        items = [w for w in warehouses.db.all()
                 if w["owner_id"] == user_id and w["status"] != "closed" and warehouses.can_see(viewer, w)]
    except Exception:
        return None
    if not items:
        return None
    lines = [f"№{w['id']} · {w['name']} — {w['region']}" for w in items[:6]]
    if len(items) > 6:
        lines.append(f"…и ещё {len(items) - 6}")
    return "\n".join(lines)


def achievements(member: discord.Member, s: dict, joined_legion: Optional[int]) -> list[str]:
    badges = []
    start = joined_legion or (int(member.joined_at.timestamp()) if member.joined_at else None)
    if start:
        days = (time.time() - start) / 86400
        for limit, badge in ((365, "🦅 Год в Легионе"), (100, "🗓️ 100 дней в Легионе"), (30, "📅 Месяц в Легионе")):
            if days >= limit:
                badges.append(badge)
                break
    came = s.get("ops_came", 0)
    for limit, badge in ((50, "⚔️ Ветеран 50 операций"), (25, "⚔️ 25 операций"), (10, "⚔️ 10 операций"),
                         (1, "⚔️ Первая операция")):
        if came >= limit:
            badges.append(badge)
            break
    ref = s.get("wh_refreshes", 0)
    for limit, badge in ((100, "🚚 Хранитель складов: 100 обновлений"), (50, "🚚 Логист: 50 обновлений"),
                         (10, "🚚 10 обновлений складов")):
        if ref >= limit:
            badges.append(badge)
            break
    if s.get("ops_led", 0) >= 5:
        badges.append("🎺 Командир: 5+ операций")
    for pos in dict.fromkeys(s.get("elect_won", [])):
        badges.append(f"🏛️ Избран: {pos}")
    th = s.get("thanks", 0)
    for limit, badge in ((50, "🤝 Опора Легиона: 50 благодарностей"), (10, "🤝 10 благодарностей")):
        if th >= limit:
            badges.append(badge)
            break
    if s.get("voice_month", 0) >= 50:
        badges.append("🎧 50+ часов в голосе за месяц")
    return badges


# ---------- карточка ----------

def profile_embed(member: discord.Member, viewer: discord.abc.User) -> discord.Embed:
    p = db.get(member.id)
    app = application(member.id)
    answers = app.get("fields", {})
    s = gather(member.id)
    joined_legion = iso_ts(app.get("reviewed_at")) if app.get("status") == "approved" else None

    color = member.color if member.color.value else discord.Color.dark_red()
    e = discord.Embed(title=f"🪪 {member.display_name}", color=color)
    e.set_thumbnail(url=member.display_avatar.url)

    vac = db.vacation(member.id)
    if vac:
        e.description = (f"🏖️ **В отпуске до <t:{vac['until']}:D>**"
                         + (f" — {vac['reason']}" if vac["reason"] else ""))

    e.add_field(name="Звание", value=rank_of(member), inline=True)
    e.add_field(name="Ник в Foxhole", value=p.get("nickname") or app.get("nickname") or "—", inline=True)
    e.add_field(name="Специализация", value=p.get("specialty") or "—", inline=True)
    e.add_field(name="Часовой пояс и онлайн", value=p.get("timezone") or answers.get("Онлайн") or "—", inline=False)

    service = []
    if member.joined_at:
        service.append(f"На сервере с <t:{int(member.joined_at.timestamp())}:D>")
    if joined_legion:
        line = f"Принят в Легион <t:{joined_legion}:D>"
        if app.get("reviewed_by"):
            line += f" · принял <@{app['reviewed_by']}>"
        service.append(line)
    path = rank_path(member.id)
    if path:
        service.append(f"Путь: {path}")
    if service:
        e.add_field(name="🎖️ Служба", value="\n".join(service)[:1024], inline=False)

    if p.get("about"):
        e.add_field(name="О себе", value=p["about"], inline=False)

    badges = achievements(member, s, joined_legion)
    if badges:
        e.add_field(name="⭐ Достижения", value="\n".join(badges)[:1024], inline=False)

    if s["awards"]:
        lines = []
        for a in s["awards"][:6]:
            emoji, name = AWARDS.get(a["award"], ("🏅", a["award"]))
            line = f"{emoji} **{name}** · <t:{a['ts']}:d>"
            if a["reason"]:
                line += f" — {a['reason']}"
            lines.append(line)
        if len(s["awards"]) > 6:
            lines.append(f"…и ещё {len(s['awards']) - 6}")
        e.add_field(name=f"🏅 Награды ({len(s['awards'])})", value="\n".join(lines)[:1024], inline=False)

    if "ops_answers" in s:
        ans = s["ops_answers"]
        signed = ans.get("going", 0) + ans.get("late", 0)
        ops = f"Пришёл: **{s.get('ops_came', 0)}**"
        if s.get("ops_marked_signed"):
            ops += f" (из {s['ops_marked_signed']} записей с отмеченной явкой)"
        ops += f"\nЗаписей: ✅ {ans.get('going', 0)} · ⏰ {ans.get('late', 0)} · ❌ {ans.get('no', 0)}"
        if s.get("ops_led"):
            ops += f"\nКомандовал: {s['ops_led']}"
        if signed or s.get("ops_came") or s.get("ops_led"):
            e.add_field(name="⚔️ Операции", value=ops, inline=False)

    if s.get("wh_created") or s.get("wh_refreshes"):
        e.add_field(name="🚚 Логистика",
                    value=f"Складов создал: {s.get('wh_created', 0)}\nОбновлений складов: {s.get('wh_refreshes', 0)}",
                    inline=True)

    civic = []
    if "votes" in s:
        civic.append(f"Голосований: {s['votes']}")
    if s.get("elect_voted"):
        civic.append(f"Выборов: {s['elect_voted']}")
    if "acks" in s:
        civic.append(f"Эдиктов прочитано: {s['acks']}")
    if civic:
        e.add_field(name="🗳️ Гражданская жизнь", value="\n".join(civic), inline=True)

    made = []
    if s.get("edicts_made"):
        made.append(f"Эдиктов издал: {s['edicts_made']}")
    if s.get("polls_made"):
        made.append(f"Голосований провёл: {s['polls_made']}")
    if s.get("elect_made"):
        made.append(f"Выборов объявил: {s['elect_made']}")
    if s.get("elect_ran"):
        made.append(f"Выдвигался на выборах: {s['elect_ran']}")
    if s.get("tasks_done"):
        made.append(f"Поручений выполнил: {s['tasks_done']}")
    if made:
        e.add_field(name="📜 Вклад в Консулат", value="\n".join(made), inline=True)

    if s["voice_month"] >= 0.1:
        e.add_field(name="🎧 В голосовых",
                    value=f"Неделя: {s['voice_week']:.1f} ч\nМесяц: {s['voice_month']:.1f} ч", inline=True)

    if s["thanks"]:
        lines = [f"Всего: **{s['thanks']}**"]
        for t in s["thanks_recent"]:
            line = f"<@{t['from_id']}>"
            if t["reason"]:
                line += f": {t['reason']}"
            lines.append(line)
        e.add_field(name="🤝 Благодарности", value="\n".join(lines)[:1024], inline=False)

    wh = warehouses_text(member.id, viewer)
    if wh:
        e.add_field(name="📦 Склады", value=wh[:1024], inline=False)

    e.set_footer(text="Senatus Populusque Legionis")
    return e


class EditProfileModal(discord.ui.Modal, title="Мой профиль"):
    def __init__(self, user_id: int) -> None:
        super().__init__()
        p = db.get(user_id)
        app = application(user_id)
        self.nickname = discord.ui.TextInput(label="Ник в Foxhole", max_length=64, required=False,
                                             default=p.get("nickname") or app.get("nickname") or None)
        self.specialty = discord.ui.TextInput(label="Специализация", max_length=100, required=False,
                                              placeholder="Логист, танкист, артиллерия, медик, пехота…",
                                              default=p.get("specialty") or None)
        self.timezone = discord.ui.TextInput(label="Часовой пояс и время онлайна", max_length=100, required=False,
                                             placeholder="Например: МСК, по вечерам 19:00–23:00",
                                             default=p.get("timezone") or app.get("fields", {}).get("Онлайн") or None)
        self.about = discord.ui.TextInput(label="О себе", style=discord.TextStyle.paragraph, max_length=500,
                                          required=False, default=p.get("about") or None)
        for item in (self.nickname, self.specialty, self.timezone, self.about):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db.save(interaction.user.id, nickname=self.nickname.value.strip(), specialty=self.specialty.value.strip(),
                timezone=self.timezone.value.strip(), about=self.about.value.strip())
        await interaction.response.edit_message(embed=profile_embed(interaction.user, interaction.user),
                                                view=OwnProfileView())


class OwnProfileView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @discord.ui.button(label="Изменить профиль", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(EditProfileModal(interaction.user.id))


async def show_profile(interaction: discord.Interaction, member: discord.Member) -> None:
    if member.bot:
        await interaction.response.send_message("У ботов нет профиля.", ephemeral=True)
        return
    own = member.id == interaction.user.id
    kwargs = {"view": OwnProfileView()} if own else {}
    text = None
    if own and not db.get(member.id):
        text = "Дополните профиль: специализация, часовой пояс, пару слов о себе — кнопка ниже."
    await interaction.response.send_message(text, embed=profile_embed(member, interaction.user), ephemeral=True,
                                            **kwargs)


@app_commands.context_menu(name="Профиль")
@app_commands.guild_only()
async def profile_menu(interaction: discord.Interaction, member: discord.Member) -> None:
    await show_profile(interaction, member)


# ---------- отпуск ----------

def parse_until(text: str) -> Optional[int]:
    """«30.11», «30.11.2026», «7д», «2 недели» → unix-время окончания."""
    from cogs.consulate import parse_duration
    from cogs.operations import local_tz
    text = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
    if m:
        day, month, year = m.groups()
        now = datetime.now(local_tz())
        y = int(year) if year else now.year
        if y < 100:
            y += 2000
        try:
            end = now.replace(year=y, month=int(month), day=int(day), hour=23, minute=59, second=0, microsecond=0)
        except ValueError:
            return None
        if not year and end < now:
            end = end.replace(year=end.year + 1)
        ts = int(end.timestamp())
    else:
        text = text.replace("недели", "н").replace("неделю", "н").replace("недель", "н")
        weeks = re.fullmatch(r"(\d+)\s*н", text)
        if weeks:
            ts = int(time.time()) + int(weeks.group(1)) * 7 * 86400
        else:
            seconds = parse_duration(text)
            if seconds is None:
                return None
            ts = int(time.time()) + seconds
    if ts <= time.time() or ts > time.time() + 120 * 86400:
        return None
    return ts


# ---------- ког ----------

AWARD_CHOICES = [app_commands.Choice(name=f"{emoji} {name}", value=key) for key, (emoji, name) in AWARDS.items()]


async def legion_chat(client: discord.Client) -> Optional[discord.abc.Messageable]:
    if not config.LEGION_CHAT_CHANNEL_ID:
        return None
    try:
        return client.get_channel(config.LEGION_CHAT_CHANNEL_ID) or await client.fetch_channel(
            config.LEGION_CHAT_CHANNEL_ID)
    except discord.HTTPException:
        return None


class Profiles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.voice_start: dict[int, float] = {}
        self.message_stamps: dict[int, int] = {}

    async def cog_load(self) -> None:
        self.flush_activity.start()

    @tasks.loop(minutes=5)
    async def flush_activity(self) -> None:
        if self.message_stamps:
            stamps, self.message_stamps = self.message_stamps, {}
            db.touch_messages(stamps)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Запоминаем только время последнего сообщения — текст не читается и не хранится."""
        if message.author.bot or message.guild is None or message.guild.id != config.GUILD_ID:
            return
        self.message_stamps[message.author.id] = int(message.created_at.timestamp())

    # --- голосовая активность ---
    def _counts(self, state: Optional[discord.VoiceState]) -> bool:
        if state is None or state.channel is None:
            return False
        afk = state.channel.guild.afk_channel
        return not (afk and state.channel.id == afk.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        now = time.time()
        for channel in guild.voice_channels + list(guild.stage_channels):
            if guild.afk_channel and channel.id == guild.afk_channel.id:
                continue
            for m in channel.members:
                if not m.bot:
                    self.voice_start.setdefault(m.id, now)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        if member.bot or member.guild.id != config.GUILD_ID:
            return
        was, now_in = self._counts(before), self._counts(after)
        if now_in and not was:
            self.voice_start[member.id] = time.time()
        elif was and not now_in:
            start = self.voice_start.pop(member.id, None)
            if start:
                db.add_voice(member.id, start, time.time())

    async def cog_unload(self) -> None:
        self.flush_activity.cancel()
        if self.message_stamps:
            db.touch_messages(self.message_stamps)
            self.message_stamps = {}
        now = time.time()
        for uid, start in self.voice_start.items():
            db.add_voice(uid, start, now)
        self.voice_start.clear()

    # --- профиль ---
    @app_commands.command(name="профиль", description="Профиль легионера")
    @app_commands.describe(участник="Чей профиль показать (по умолчанию — ваш)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, участник: Optional[discord.Member] = None) -> None:
        await show_profile(interaction, участник or interaction.user)

    # --- награды ---
    @app_commands.command(name="наградить", description="Вручить награду легионеру")
    @app_commands.describe(участник="Кого наградить", награда="Какая награда", причина="За что")
    @app_commands.choices(награда=AWARD_CHOICES)
    @app_commands.guild_only()
    async def give_award(self, interaction: discord.Interaction, участник: discord.Member,
                         награда: app_commands.Choice[str], причина: str) -> None:
        if not can_award(interaction.user):
            await interaction.response.send_message("⛔ Награды вручают Принцепс, Консулы, Сенат и Легаты.",
                                                    ephemeral=True)
            return
        if участник.bot:
            await interaction.response.send_message("Ботов не награждают.", ephemeral=True)
            return
        aid = db.add_award(участник.id, награда.value, причина[:300], interaction.user.id)
        emoji, name = AWARDS[награда.value]
        await interaction.response.send_message(f"✅ Награда №{aid} вручена {участник.mention}.", ephemeral=True)
        text = (f"🏅 **НАГРАДА ЛЕГИОНА**\n\n{участник.mention} награждён: {emoji} **{name}**\n"
                f"За: {причина[:300]}\n\nВручил: {interaction.user.mention}")
        channel = await legion_chat(self.bot)
        if channel:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[участник]))
            except discord.HTTPException:
                pass
        from cogs.consulate import archive
        await archive(self.bot, f"🏅 <t:{int(time.time())}:f> · {interaction.user.mention} вручил {участник.mention} "
                                f"награду №{aid}: {emoji} {name} — {причина[:300]}")

    @app_commands.command(name="награда_снять", description="Снять ошибочно выданную награду")
    @app_commands.describe(номер="Номер награды (из сообщения о вручении)")
    @app_commands.guild_only()
    async def remove_award(self, interaction: discord.Interaction, номер: int) -> None:
        a = db.get_award(номер)
        if a is None:
            await interaction.response.send_message("Награда не найдена.", ephemeral=True)
            return
        from cogs.consulate import can_create
        if not (can_create(interaction.user) or interaction.user.id == a["by_id"]):
            await interaction.response.send_message("⛔ Снять награду может вручивший или Принцепс, Консулы, Сенат.",
                                                    ephemeral=True)
            return
        db.remove_award(номер)
        await interaction.response.send_message(f"✅ Награда №{номер} снята.", ephemeral=True)
        from cogs.consulate import archive
        await archive(self.bot, f"🗑️ <t:{int(time.time())}:f> · {interaction.user.mention} снял награду №{номер} "
                                f"у <@{a['user_id']}>")

    # --- благодарности ---
    @app_commands.command(name="спасибо", description="Поблагодарить легионера (раз в сутки)")
    @app_commands.describe(участник="Кого благодарите", за_что="За что")
    @app_commands.guild_only()
    async def thanks(self, interaction: discord.Interaction, участник: discord.Member, за_что: str) -> None:
        if участник.id == interaction.user.id or участник.bot:
            await interaction.response.send_message("Поблагодарить можно только другого легионера.", ephemeral=True)
            return
        last = db.last_thanks_from(interaction.user.id)
        if last and time.time() - last < 86400:
            await interaction.response.send_message(
                f"Благодарить можно раз в сутки. Следующая — <t:{last + 86400}:R>.", ephemeral=True)
            return
        db.add_thanks(interaction.user.id, участник.id, за_что[:200])
        await interaction.response.send_message(
            f"🤝 {interaction.user.mention} благодарит {участник.mention}: {за_что[:200]}",
            allowed_mentions=discord.AllowedMentions(users=[участник]),
        )

    # --- отпуск ---
    @app_commands.command(name="отпуск", description="Отметить, что вы отсутствуете")
    @app_commands.describe(до="До какого числа: 30.11, или срок: 7д, 2 недели", причина="Необязательно")
    @app_commands.guild_only()
    async def vacation(self, interaction: discord.Interaction, до: str, причина: Optional[str] = None) -> None:
        until = parse_until(до)
        if until is None:
            await interaction.response.send_message(
                "Не понял дату. Примеры: `30.11`, `30.11.2026`, `7д`, `2 недели` (не больше 120 дней).",
                ephemeral=True)
            return
        db.set_vacation(interaction.user.id, until, (причина or "")[:200])
        await interaction.response.send_message(
            f"🏖️ Отмечено: вы в отпуске до <t:{until}:D>. Вернётесь раньше — `/вернулся`.", ephemeral=True)

    @app_commands.command(name="вернулся", description="Закончить отпуск")
    @app_commands.guild_only()
    async def back(self, interaction: discord.Interaction) -> None:
        if db.end_vacation(interaction.user.id):
            await interaction.response.send_message("⚔️ С возвращением в строй!", ephemeral=True)
        else:
            await interaction.response.send_message("Вы не в отпуске.", ephemeral=True)

    @app_commands.command(name="отпуска", description="Кто сейчас в отпуске")
    @app_commands.guild_only()
    async def vacations(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message("⛔ Список видят офицеры.", ephemeral=True)
            return
        items = db.vacations()
        if not items:
            await interaction.response.send_message("Сейчас никто не в отпуске.", ephemeral=True)
            return
        lines = [f"<@{v['user_id']}> — до <t:{v['until']}:D>" + (f" · {v['reason']}" if v["reason"] else "")
                 for v in items[:40]]
        await interaction.response.send_message(
            embed=discord.Embed(title=f"🏖️ В отпуске: {len(items)}", description="\n".join(lines)[:4000],
                                color=discord.Color.dark_red()), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    await bot.add_cog(Profiles(bot))
    bot.tree.add_command(profile_menu)
