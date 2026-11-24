"""Колизей: дуэли, Арена, титулы гладиаторов, турниры, вопрос дня.

Дуэль идёт в закрытой ветке, которую видят только двое бойцов. После боя ветка удаляется,
а в Колизее остаётся одно короткое сообщение с итогом.

Ходы каждый раунд (тайно): ⚔️ Удар бьёт 🏹 Пилум, 🏹 Пилум пробивает 🛡️ Щит,
🛡️ Щит блокирует ⚔️ Удар и даёт контратаку. 100 HP, до 10 раундов, минута на ход.

Титулы по победам: Тирон → Гладиатор → Ветеран арены → Примус палус → Легенда Колизея.
Турниры: объявляют Принцепс, Консулы, Сенат; победитель получает роль «Чемпион Арены».
Вопрос дня: каждый день в 18:00 (TZ_OFFSET), первый верный ответ — 3 очка, остальные — 1.
/колизей — закреплённая панель. Данные — в отдельном файле data/colosseum.db.
"""
import asyncio
import logging
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.consulate import can_create, has_any, is_admin, legion_roles
from cogs.colosseum_quiz import QUESTIONS

log = logging.getLogger("colosseum")

DB_PATH: Path = config.DATA_DIR / "colosseum.db"
HP = 100
MAX_ROUNDS = 10
MOVE_TIMEOUT = 60
INVITE_TIMEOUT = 300
QUIZ_HOUR = 18
CHAMPION_ROLE = "Чемпион Арены"

MOVES = {"strike": ("⚔️", "Удар"), "shield": ("🛡️", "Щит"), "pilum": ("🏹", "Пилум")}
TITLES = [(50, "👑 Легенда Колизея"), (30, "🦁 Примус палус"), (15, "🗡️ Ветеран арены"),
          (5, "⚔️ Гладиатор"), (1, "🔰 Тирон")]


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS duels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                a_id          INTEGER NOT NULL,
                b_id          INTEGER NOT NULL,
                winner_id     INTEGER,          -- NULL — ничья
                result        TEXT    NOT NULL,  -- win / draw / declined / noshow
                rounds        INTEGER NOT NULL DEFAULT 0,
                a_hp          INTEGER NOT NULL DEFAULT 0,
                b_hp          INTEGER NOT NULL DEFAULT 0,
                tournament_id INTEGER,
                ts            INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tournaments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER,
                starts_at   INTEGER NOT NULL,
                author_id   INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'signup',   -- signup / running / done / cancelled
                round       INTEGER NOT NULL DEFAULT 0,
                winner_id   INTEGER
            );
            CREATE TABLE IF NOT EXISTS t_players (
                tournament_id INTEGER NOT NULL,
                user_id       INTEGER NOT NULL,
                alive         INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (tournament_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS t_matches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                round         INTEGER NOT NULL,
                a_id          INTEGER NOT NULL,
                b_id          INTEGER,                         -- NULL — проход без боя
                winner_id     INTEGER,
                status        TEXT NOT NULL DEFAULT 'pending'  -- pending / running / done
            );
            CREATE TABLE IF NOT EXISTS quiz_custom (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                correct  TEXT NOT NULL,
                wrong    TEXT NOT NULL                          -- через «|»
            );
            CREATE TABLE IF NOT EXISTS quiz_posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                qkey       TEXT    NOT NULL,
                question   TEXT    NOT NULL,
                options    TEXT    NOT NULL,                    -- через «|»
                correct    INTEGER NOT NULL,                    -- индекс верного
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                posted_at  INTEGER NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS quiz_answers (
                post_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                points  INTEGER NOT NULL,
                ts      INTEGER NOT NULL,
                PRIMARY KEY (post_id, user_id)
            );
            """
        )
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

    def record(self, user_id: int) -> tuple[int, int, int]:
        wins = self.q("SELECT COUNT(*) AS n FROM duels WHERE winner_id = ?", (user_id,))[0]["n"]
        losses = self.q("SELECT COUNT(*) AS n FROM duels WHERE result = 'win' AND winner_id != ? "
                        "AND (a_id = ? OR b_id = ?)", (user_id, user_id, user_id))[0]["n"]
        draws = self.q("SELECT COUNT(*) AS n FROM duels WHERE result = 'draw' AND (a_id = ? OR b_id = ?)",
                       (user_id, user_id))[0]["n"]
        return wins, losses, draws


db: Optional[DB] = None
busy: set[int] = set()   # кто сейчас в дуэли


def title_for(wins: int) -> Optional[str]:
    for need, title in TITLES:
        if wins >= need:
            return title
    return None


def colosseum_channel(client: discord.Client) -> Optional[discord.TextChannel]:
    cid = db.meta("channel_id")
    return client.get_channel(int(cid)) if cid else None


def is_legion(member: discord.abc.User) -> bool:
    return is_admin(member) or has_any(member, legion_roles())


async def say(interaction: discord.Interaction, text: str = None, **kw) -> None:
    kw.setdefault("ephemeral", True)
    kw.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if interaction.response.is_done():
        await interaction.followup.send(text, **kw)
    else:
        await interaction.response.send_message(text, **kw)


# ================= ДУЭЛЬ =================

def resolve(a_name: str, b_name: str, a_move: str, b_move: str) -> tuple[int, int, str]:
    """Возвращает (урон по A, урон по B, описание раунда)."""
    def dmg(lo: int, hi: int) -> tuple[int, str]:
        d = random.randint(lo, hi)
        if random.random() < 0.1:
            return int(d * 1.5), " **Критический удар!**"
        return d, ""

    if random.random() < 0.06:
        return 0, 0, f"Оба поскользнулись на песке арены. Толпа смеётся. 😄"

    pair = (a_move, b_move)
    if a_move == b_move:
        if a_move == "strike":
            da, ca = dmg(8, 16)
            db_, cb = dmg(8, 16)
            return da, db_, f"Гладиусы скрестились! {a_name} (−{da}){ca}, {b_name} (−{db_}){cb}."
        if a_move == "pilum":
            da, ca = dmg(6, 12)
            db_, cb = dmg(6, 12)
            return da, db_, f"Пилумы летят навстречу! {a_name} (−{da}){ca}, {b_name} (−{db_}){cb}."
        return 0, 0, "Оба прикрылись скутумами и кружат по арене…"
    wins = {("strike", "pilum"), ("pilum", "shield"), ("shield", "strike")}
    if pair in wins:
        attacker, defender, move = a_name, b_name, a_move
        d, c = dmg(*{"strike": (18, 28), "pilum": (16, 26), "shield": (10, 18)}[move])
        target_is_b = True
    else:
        attacker, defender, move = b_name, a_name, b_move
        d, c = dmg(*{"strike": (18, 28), "pilum": (16, 26), "shield": (10, 18)}[move])
        target_is_b = False
    text = {
        "strike": f"{attacker} сокращает дистанцию — удар гладиусом по {defender}! (−{d}){c}",
        "pilum": f"Пилум {attacker} пробивает щит {defender}! (−{d}){c}",
        "shield": f"{attacker} принимает удар на скутум и контратакует {defender}! (−{d}){c}",
    }[move]
    return (0, d, text) if target_is_b else (d, 0, text)


class MoveView(discord.ui.View):
    def __init__(self, duel: "Duel") -> None:
        super().__init__(timeout=MOVE_TIMEOUT + 10)
        self.duel = duel
        for key, (emoji, label) in MOVES.items():
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary)
            btn.callback = self._make(key)
            self.add_item(btn)

    def _make(self, key: str):
        async def cb(interaction: discord.Interaction) -> None:
            d = self.duel
            if interaction.user.id not in (d.a.id, d.b.id):
                await say(interaction, "Это не твой бой. Смотри с трибун. 🍿")
                return
            if interaction.user.id in d.moves:
                await say(interaction, "Ход уже сделан — ждём соперника.")
                return
            d.moves[interaction.user.id] = key
            await say(interaction, f"{MOVES[key][0]} Ход принят: **{MOVES[key][1]}**.")
            if len(d.moves) == 2:
                d.both_moved.set()
        return cb


class Duel:
    def __init__(self, client: discord.Client, a: discord.Member, b: discord.Member,
                 thread: discord.Thread, tournament_id: Optional[int] = None) -> None:
        self.client = client
        self.a, self.b = a, b
        self.thread = thread
        self.tournament_id = tournament_id
        self.hp = {a.id: HP, b.id: HP}
        self.moves: dict[int, str] = {}
        self.both_moved = asyncio.Event()
        self.best = (0, "")

    def bars(self) -> str:
        def bar(hp: int) -> str:
            filled = round(max(hp, 0) / HP * 10)
            return "❤️" * filled + "🖤" * (10 - filled)
        return (f"**{self.a.display_name}** {bar(self.hp[self.a.id])} {max(self.hp[self.a.id], 0)}\n"
                f"**{self.b.display_name}** {bar(self.hp[self.b.id])} {max(self.hp[self.b.id], 0)}")

    async def run(self) -> Optional[int]:
        """Проводит бой. Возвращает id победителя (или None при ничьей)."""
        rounds = 0
        for rnd in range(1, MAX_ROUNDS + 1):
            rounds = rnd
            self.moves = {}
            self.both_moved = asyncio.Event()
            view = MoveView(self)
            msg = await self.thread.send(f"**Раунд {rnd}** — выбирайте ход (минута)!\n{self.bars()}", view=view)
            try:
                await asyncio.wait_for(self.both_moved.wait(), timeout=MOVE_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            for p in (self.a, self.b):
                if p.id not in self.moves:
                    self.moves[p.id] = random.choice(list(MOVES))
            view.stop()
            am, bm = self.moves[self.a.id], self.moves[self.b.id]
            da, dbb, text = resolve(self.a.display_name, self.b.display_name, am, bm)
            self.hp[self.a.id] -= da
            self.hp[self.b.id] -= dbb
            if max(da, dbb) > self.best[0]:
                self.best = (max(da, dbb), text)
            await msg.edit(content=f"**Раунд {rnd}:** {MOVES[am][0]} vs {MOVES[bm][0]}\n{text}\n{self.bars()}",
                           view=None)
            if self.hp[self.a.id] <= 0 or self.hp[self.b.id] <= 0:
                break
            await asyncio.sleep(1)
        ha, hb = self.hp[self.a.id], self.hp[self.b.id]
        if ha == hb:
            winner = None
        else:
            winner = self.a.id if ha > hb else self.b.id
        db.x("INSERT INTO duels (a_id, b_id, winner_id, result, rounds, a_hp, b_hp, tournament_id, ts) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (self.a.id, self.b.id, winner, "win" if winner else "draw", rounds, max(ha, 0), max(hb, 0),
              self.tournament_id, int(time.time())))
        self.rounds = rounds
        return winner

    def summary(self, winner: Optional[int]) -> str:
        if winner is None:
            return (f"🏟️ **{self.a.display_name}** и **{self.b.display_name}** бились {self.rounds} раундов — "
                    f"ничья! Толпа требует реванша.")
        w, l = (self.a, self.b) if winner == self.a.id else (self.b, self.a)
        text = (f"🏟️ **{w.display_name}** одолел **{l.display_name}** за {self.rounds} "
                f"раунд(а), осталось {max(self.hp[w.id], 0)} HP.")
        if self.best[0]:
            text += f"\nЛучший момент: {self.best[1]}"
        text += "\nТолпа ревёт: «Ave!»"
        wins = db.record(w.id)[0]
        new_title = title_for(wins)
        if new_title and title_for(wins - 1) != new_title:
            text += f"\n🎖️ {w.mention} получает титул **{new_title}**!"
        return text


async def make_thread(channel: discord.TextChannel, a: discord.Member, b: discord.Member,
                      name: str) -> discord.Thread:
    thread = await channel.create_thread(name=name[:90], type=discord.ChannelType.private_thread, invitable=False,
                                         auto_archive_duration=60)
    await thread.add_user(a)
    await thread.add_user(b)
    return thread


async def close_thread(thread: discord.Thread, delay: int = 20) -> None:
    await asyncio.sleep(delay)
    try:
        await thread.delete()
    except discord.HTTPException:
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            pass


async def run_duel(client: discord.Client, a: discord.Member, b: discord.Member, thread: discord.Thread,
                   tournament_id: Optional[int] = None) -> Optional[int]:
    busy.update({a.id, b.id})
    try:
        duel = Duel(client, a, b, thread, tournament_id)
        await thread.send(f"⚔️ **{a.display_name}** против **{b.display_name}**! Да начнётся бой!\n"
                          "⚔️ Удар бьёт 🏹 Пилум · 🏹 Пилум пробивает 🛡️ Щит · 🛡️ Щит блокирует ⚔️ Удар.")
        winner = await duel.run()
        text = duel.summary(winner)
        await thread.send(text + "\n\n*Ветка закроется через 20 секунд.*")
        channel = colosseum_channel(client) or thread.parent
        if channel:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        asyncio.create_task(close_thread(thread))
        return winner
    finally:
        busy.difference_update({a.id, b.id})


class InviteView(discord.ui.View):
    def __init__(self, client: discord.Client, a: discord.Member, b: discord.Member, thread: discord.Thread) -> None:
        super().__init__(timeout=INVITE_TIMEOUT)
        self.client = client
        self.a, self.b, self.thread = a, b, thread
        self.answered = False

    async def on_timeout(self) -> None:
        if self.answered:
            return
        busy.difference_update({self.a.id, self.b.id})
        db.x("INSERT INTO duels (a_id, b_id, result, ts) VALUES (?, ?, 'noshow', ?)",
             (self.a.id, self.b.id, int(time.time())))
        channel = colosseum_channel(self.client) or self.thread.parent
        if channel:
            try:
                await channel.send(f"🕯️ **{self.b.display_name}** не явился на вызов **{self.a.display_name}**. "
                                   "Арена ждала напрасно.")
            except discord.HTTPException:
                pass
        await close_thread(self.thread, 0)

    @discord.ui.button(label="Принять", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.b.id:
            await say(interaction, "Вызов адресован не тебе.")
            return
        self.answered = True
        self.stop()
        await interaction.response.edit_message(content=f"⚔️ **{self.b.display_name}** принимает вызов!", view=None)
        asyncio.create_task(run_duel(interaction.client, self.a, self.b, self.thread))

    @discord.ui.button(label="Струсить", emoji="🏃", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.b.id:
            await say(interaction, "Вызов адресован не тебе.")
            return
        self.answered = True
        self.stop()
        busy.difference_update({self.a.id, self.b.id})
        db.x("INSERT INTO duels (a_id, b_id, result, ts) VALUES (?, ?, 'declined', ?)",
             (self.a.id, self.b.id, int(time.time())))
        await interaction.response.edit_message(content="🏃 Вызов отклонён.", view=None)
        channel = colosseum_channel(interaction.client) or self.thread.parent
        if channel:
            await channel.send(f"🏃 **{self.b.display_name}** струсил перед **{self.a.display_name}**. "
                               "Толпа свистит! 🍅")
        asyncio.create_task(close_thread(self.thread, 5))


async def challenge(interaction: discord.Interaction, b: discord.abc.User, edit: bool) -> None:
    """Бросить вызов: открыть закрытую ветку и отправить приглашение."""
    async def reply(text: str) -> None:
        if edit:
            await interaction.response.edit_message(content=text, view=None)
        else:
            await say(interaction, text)

    a = interaction.user
    if b.bot or b.id == a.id:
        await reply("Выбери живого соперника. 🙂")
        return
    if not isinstance(b, discord.Member) or not is_legion(b):
        await reply("Вызывать можно только легионеров.")
        return
    if a.id in busy or b.id in busy:
        await reply("Кто-то из вас уже на арене. Подожди конца боя.")
        return
    channel = colosseum_channel(interaction.client) or interaction.channel
    try:
        thread = await make_thread(channel, a, b, f"⚔️ {a.display_name} vs {b.display_name}")
    except discord.HTTPException:
        await reply("⚠️ Не удалось открыть арену: дайте боту права «Создавать приватные ветки» "
                    "и «Управлять ветками» в Колизее.")
        return
    busy.update({a.id, b.id})
    await reply(f"⚔️ Вызов брошен! Арена: {thread.jump_url}")
    await thread.send(f"{b.mention}, тебя вызывает на бой {a.mention}! У тебя 5 минут.",
                      view=InviteView(interaction.client, a, b, thread),
                      allowed_mentions=discord.AllowedMentions(users=True))


class ChallengeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Кого вызываешь на бой?")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await challenge(interaction, select.values[0], edit=True)


# ================= АРЕНА =================

def arena_embed(viewer: discord.abc.User) -> discord.Embed:
    rows = db.q("SELECT winner_id AS u, COUNT(*) AS w FROM duels WHERE result = 'win' "
                "GROUP BY winner_id ORDER BY w DESC LIMIT 10")
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        wins, losses, _ = db.record(r["u"])
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} <@{r['u']}> — **{wins}** побед / {losses} пораж. · {title_for(wins) or ''}")
    e = discord.Embed(title="🏆 АРЕНА — лучшие бойцы", description="\n".join(lines) or "Арена пока пуста.",
                      color=discord.Color.gold())
    w, l, d = db.record(viewer.id)
    e.add_field(name="Ты", value=f"{w} побед · {l} пораж. · {d} ничьих · {title_for(w) or 'ещё без титула'}",
                inline=False)
    champ = db.meta("champion")
    if champ:
        e.add_field(name="👑 Чемпион Арены", value=f"<@{champ}>", inline=False)
    return e


# ================= ТУРНИР =================

def t_embed(t: dict) -> discord.Embed:
    players = db.q("SELECT user_id, alive FROM t_players WHERE tournament_id = ?", (t["id"],))
    state = {"signup": "📝 Запись открыта", "running": f"⚔️ Идёт раунд {t['round']}",
             "done": "🏁 Завершён", "cancelled": "🚫 Отменён"}[t["status"]]
    lines = [f"{state} · старт <t:{t['starts_at']}:f> (<t:{t['starts_at']}:R>)",
             f"Бойцов: **{len(players)}**"]
    if players:
        names = " ".join(("" if p["alive"] or t["status"] == "signup" else "~~") + f"<@{p['user_id']}>"
                         + ("" if p["alive"] or t["status"] == "signup" else "~~") for p in players)
        lines.append(names[:1500])
    if t["winner_id"]:
        lines.append(f"\n👑 Победитель: <@{t['winner_id']}> — **{CHAMPION_ROLE}**!")
    return discord.Embed(title=f"🦁 ТУРНИР: {t['name']}", description="\n".join(lines),
                         color=discord.Color.dark_red())


class TButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tour:(?P<tid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, tid: int, act: str, label: Optional[str] = None, emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"tour:{tid}:{act}"))
        self.tid = tid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["tid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        t = db.q("SELECT * FROM tournaments WHERE id = ?", (self.tid,))
        if not t or t[0]["status"] != "signup":
            await say(interaction, "Запись на турнир закрыта.")
            return
        t = t[0]
        if self.act == "join":
            if not is_legion(interaction.user):
                await say(interaction, "⛔ На арену выходят легионеры.")
                return
            db.x("INSERT OR IGNORE INTO t_players (tournament_id, user_id) VALUES (?, ?)", (t["id"], interaction.user.id))
        elif self.act == "leave":
            db.x("DELETE FROM t_players WHERE tournament_id = ? AND user_id = ?", (t["id"], interaction.user.id))
        elif self.act == "cancel":
            if interaction.user.id != t["author_id"] and not can_create(interaction.user):
                await say(interaction, "⛔ Отменить может организатор.")
                return
            db.x("UPDATE tournaments SET status = 'cancelled' WHERE id = ?", (t["id"],))
        t = db.q("SELECT * FROM tournaments WHERE id = ?", (self.tid,))[0]
        await interaction.response.edit_message(embed=t_embed(t), view=t_view(t))


def t_view(t: dict) -> Optional[discord.ui.View]:
    if t["status"] != "signup":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(TButton(t["id"], "join", "Записаться", "📝", discord.ButtonStyle.success))
    view.add_item(TButton(t["id"], "leave", None, "↩️"))
    view.add_item(TButton(t["id"], "cancel", None, "🚫"))
    return view


class TournamentForm(discord.ui.Modal, title="Турнир гладиаторов"):
    name = discord.ui.TextInput(label="Название", max_length=60, default="Игры в честь Легиона")
    when = discord.ui.TextInput(label="Старт", max_length=30, placeholder="21:00 · 30.11 20:00 · через 1ч")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from cogs.operations import parse_start
        if not can_create(interaction.user):
            await say(interaction, "⛔ Турниры объявляют Принцепс, Консулы и Сенат.")
            return
        starts = parse_start(self.when.value)
        if starts is None:
            await say(interaction, "Не понял время. Примеры: `21:00`, `30.11 20:00`, `через 1ч`.")
            return
        channel = colosseum_channel(interaction.client) or interaction.channel
        tid = db.x("INSERT INTO tournaments (name, channel_id, starts_at, author_id) VALUES (?, ?, ?, ?)",
                   (self.name.value.strip(), channel.id, starts, interaction.user.id))
        t = db.q("SELECT * FROM tournaments WHERE id = ?", (tid,))[0]
        msg = await channel.send(embed=t_embed(t), view=t_view(t))
        db.x("UPDATE tournaments SET message_id = ? WHERE id = ?", (msg.id, tid))
        await say(interaction, f"🦁 Турнир объявлен: {msg.jump_url}")


async def refresh_t(client: discord.Client, tid: int) -> None:
    t = db.q("SELECT * FROM tournaments WHERE id = ?", (tid,))[0]
    channel = client.get_channel(t["channel_id"])
    if channel and t["message_id"]:
        try:
            await channel.get_partial_message(t["message_id"]).edit(embed=t_embed(t), view=t_view(t))
        except discord.HTTPException:
            pass


async def start_round(client: discord.Client, tid: int, players: list[int]) -> None:
    t = db.q("SELECT * FROM tournaments WHERE id = ?", (tid,))[0]
    rnd = t["round"] + 1
    db.x("UPDATE tournaments SET round = ?, status = 'running' WHERE id = ?", (rnd, tid))
    random.shuffle(players)
    channel = client.get_channel(t["channel_id"])
    guild = channel.guild
    pairs = []
    while len(players) >= 2:
        pairs.append((players.pop(), players.pop()))
    if players:  # нечётный — проходит дальше без боя
        bye = players.pop()
        db.x("INSERT INTO t_matches (tournament_id, round, a_id, b_id, winner_id, status) VALUES (?, ?, ?, NULL, ?, 'done')",
             (tid, rnd, bye, bye))
        await channel.send(f"🍀 <@{bye}> проходит в следующий раунд без боя.",
                           allowed_mentions=discord.AllowedMentions.none())
    lines = []
    for a_id, b_id in pairs:
        mid = db.x("INSERT INTO t_matches (tournament_id, round, a_id, b_id, status) VALUES (?, ?, ?, ?, 'running')",
                   (tid, rnd, a_id, b_id))
        a, b = guild.get_member(a_id), guild.get_member(b_id)
        if a is None or b is None:  # кто-то ушёл с сервера — техническая победа
            winner = a_id if a else b_id
            db.x("UPDATE t_matches SET winner_id = ?, status = 'done' WHERE id = ?", (winner, mid))
            continue
        lines.append(f"⚔️ <@{a_id}> vs <@{b_id}>")
        asyncio.create_task(tournament_match(client, tid, mid, a, b, channel))
    await refresh_t(client, tid)
    if lines:
        await channel.send(f"🦁 **{t['name']} — раунд {rnd}!**\n" + "\n".join(lines) +
                           "\nБои идут в закрытых ветках, ходы — минута на раунд.",
                           allowed_mentions=discord.AllowedMentions(users=True))
    await maybe_next_round(client, tid)


async def tournament_match(client: discord.Client, tid: int, mid: int, a: discord.Member, b: discord.Member,
                           channel: discord.TextChannel) -> None:
    try:
        thread = await make_thread(channel, a, b, f"🦁 Турнир: {a.display_name} vs {b.display_name}")
        await thread.send(f"{a.mention} {b.mention} — турнирный бой! Отказаться нельзя. Начинаем через 30 секунд.",
                          allowed_mentions=discord.AllowedMentions(users=True))
        await asyncio.sleep(30)
        winner = await run_duel(client, a, b, thread, tid)
        if winner is None:  # ничья в турнире — решает жребий
            winner = random.choice([a.id, b.id])
            await channel.send(f"🎲 Ничья! Жребий отдаёт победу <@{winner}>.",
                               allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Турнирный бой %s", mid)
        winner = random.choice([a.id, b.id])
    loser = b.id if winner == a.id else a.id
    db.x("UPDATE t_matches SET winner_id = ?, status = 'done' WHERE id = ?", (winner, mid))
    db.x("UPDATE t_players SET alive = 0 WHERE tournament_id = ? AND user_id = ?", (tid, loser))
    await refresh_t(client, tid)
    await maybe_next_round(client, tid)


_t_locks: dict[int, asyncio.Lock] = {}


async def maybe_next_round(client: discord.Client, tid: int) -> None:
    """Если все бои раунда сыграны — следующий раунд или финал (защищено от двойного запуска)."""
    lock = _t_locks.setdefault(tid, asyncio.Lock())
    async with lock:
        t = db.q("SELECT * FROM tournaments WHERE id = ?", (tid,))[0]
        if t["status"] != "running":
            return
        matches = db.q("SELECT * FROM t_matches WHERE tournament_id = ? AND round = ?", (tid, t["round"]))
        if not matches or any(m["status"] != "done" for m in matches):
            return
        winners = [m["winner_id"] for m in matches]
        if len(winners) == 1:
            await finish_tournament(client, tid, winners[0])
            return
    await start_round(client, tid, winners)


async def finish_tournament(client: discord.Client, tid: int, winner_id: int) -> None:
    db.x("UPDATE tournaments SET status = 'done', winner_id = ? WHERE id = ?", (winner_id, tid))
    t = db.q("SELECT * FROM tournaments WHERE id = ?", (tid,))[0]
    channel = client.get_channel(t["channel_id"])
    guild = channel.guild
    note = ""
    try:
        role = discord.utils.get(guild.roles, name=CHAMPION_ROLE)
        if role is None:
            role = await guild.create_role(name=CHAMPION_ROLE, colour=discord.Colour.gold(), hoist=True,
                                           reason="Чемпион Арены")
        for m in list(role.members):
            if m.id != winner_id:
                await m.remove_roles(role, reason="Новый чемпион")
        member = guild.get_member(winner_id)
        if member:
            await member.add_roles(role, reason=f"Победа в турнире №{tid}")
    except discord.HTTPException:
        note = "\n(роль выдать не удалось — проверьте права бота «Управлять ролями»)"
    db.set_meta("champion", str(winner_id))
    await refresh_t(client, tid)
    await channel.send(f"👑 **{t['name']} завершён!** Победитель — <@{winner_id}>, новый **{CHAMPION_ROLE}**! "
                       f"Толпа бросает розы на арену! 🌹{note}", allowed_mentions=discord.AllowedMentions(users=True))


# ================= ВОПРОС ДНЯ =================

def pick_question() -> tuple[str, str, str, list[str]]:
    used = {r["qkey"] for r in db.q("SELECT qkey FROM quiz_posts")}
    pool = [(f"b{i}", q, c, w) for i, (q, c, w) in enumerate(QUESTIONS)]
    pool += [(f"c{r['id']}", r["question"], r["correct"], r["wrong"].split("|"))
             for r in db.q("SELECT * FROM quiz_custom")]
    fresh = [p for p in pool if p[0] not in used]
    custom = [p for p in fresh if p[0].startswith("c")]
    if custom:  # свои вопросы командования — в первую очередь, по порядку добавления
        return min(custom, key=lambda p: int(p[0][1:]))
    if not fresh:  # все вопросы были — начинаем круг заново, но не повторяем последние 10
        recent = {r["qkey"] for r in db.q("SELECT qkey FROM quiz_posts ORDER BY id DESC LIMIT 10")}
        fresh = [p for p in pool if p[0] not in recent] or pool
    return random.choice(fresh)


def quiz_embed(p: dict, closed: bool = False) -> discord.Embed:
    options = p["options"].split("|")
    letters = ["🇦", "🇧", "🇨", "🇩"]
    lines = [f"**{p['question']}**", ""]
    for i, o in enumerate(options):
        mark = " ✅" if closed and i == p["correct"] else ""
        lines.append(f"{letters[i]} {o}{mark}")
    e = discord.Embed(title="🧠 ВОПРОС ДНЯ", description="\n".join(lines), color=discord.Color.teal())
    if closed:
        answers = db.q("SELECT * FROM quiz_answers WHERE post_id = ? ORDER BY ts", (p["id"],))
        right = [a for a in answers if a["correct"]]
        first = f"первым ответил <@{right[0]['user_id']}>, " if right else ""
        e.add_field(name="Итог", value=f"{first}верно ответили {len(right)} из {len(answers)}.", inline=False)
    else:
        e.set_footer(text="Одна попытка. Первый верный — 3 очка, остальные верные — 1. Ответ откроется завтра.")
    return e


class QuizButton(discord.ui.DynamicItem[discord.ui.Button], template=r"quiz:(?P<pid>[0-9]+):(?P<idx>[0-9])"):
    def __init__(self, pid: int, idx: int, emoji: Optional[str] = None) -> None:
        super().__init__(discord.ui.Button(emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"quiz:{pid}:{idx}"))
        self.pid = pid
        self.idx = idx

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["pid"]), int(match["idx"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        rows = db.q("SELECT * FROM quiz_posts WHERE id = ?", (self.pid,))
        if not rows or rows[0]["status"] != "open":
            await say(interaction, "Этот вопрос уже закрыт — ждём следующий!")
            return
        p = rows[0]
        if db.q("SELECT 1 FROM quiz_answers WHERE post_id = ? AND user_id = ?", (p["id"], interaction.user.id)):
            await say(interaction, "Ты уже отвечал на этот вопрос. 😉")
            return
        correct = self.idx == p["correct"]
        points = 0
        if correct:
            first = not db.q("SELECT 1 FROM quiz_answers WHERE post_id = ? AND correct = 1", (p["id"],))
            points = 3 if first else 1
        db.x("INSERT INTO quiz_answers VALUES (?, ?, ?, ?, ?)",
             (p["id"], interaction.user.id, int(correct), points, int(time.time())))
        if correct:
            await say(interaction, f"✅ Верно! +{points}" + (" — ты первый! 🥇" if points == 3 else ""))
        else:
            await say(interaction, "❌ Увы, неверно. Правильный ответ откроется завтра.")


def quiz_view(p: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i, letter in enumerate(["🇦", "🇧", "🇨", "🇩"][:len(p["options"].split("|"))]):
        view.add_item(QuizButton(p["id"], i, letter))
    return view


async def post_quiz(client: discord.Client) -> Optional[str]:
    channel = colosseum_channel(client)
    if channel is None:
        return "Канал Колизея не известен — опубликуйте панель командой /колизей."
    for old in db.q("SELECT * FROM quiz_posts WHERE status = 'open'"):
        db.x("UPDATE quiz_posts SET status = 'closed' WHERE id = ?", (old["id"],))
        old["status"] = "closed"
        try:
            ch = client.get_channel(old["channel_id"])
            if ch:
                await ch.get_partial_message(old["message_id"]).edit(embed=quiz_embed(old, closed=True), view=None)
        except discord.HTTPException:
            pass
    key, question, correct, wrong = pick_question()
    options = wrong[:3] + [correct]
    random.shuffle(options)
    pid = db.x("INSERT INTO quiz_posts (qkey, question, options, correct, channel_id, posted_at) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (key, question, "|".join(options), options.index(correct), channel.id, int(time.time())))
    p = db.q("SELECT * FROM quiz_posts WHERE id = ?", (pid,))[0]
    msg = await channel.send(embed=quiz_embed(p), view=quiz_view(p))
    db.x("UPDATE quiz_posts SET message_id = ? WHERE id = ?", (msg.id, pid))
    return None


def scholars_embed(viewer: discord.abc.User) -> discord.Embed:
    rows = db.q("SELECT user_id, SUM(points) AS pts, SUM(correct) AS ok FROM quiz_answers "
                "GROUP BY user_id ORDER BY pts DESC LIMIT 10")
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else str(i + 1) + '.'} <@{r['user_id']}> — **{r['pts']}** очк. "
             f"({r['ok']} верных)" for i, r in enumerate(rows)]
    e = discord.Embed(title="🧠 ЗНАТОКИ ЛЕГИОНА", description="\n".join(lines) or "Пока никто не отвечал.",
                      color=discord.Color.teal())
    mine = db.q("SELECT COALESCE(SUM(points), 0) AS pts FROM quiz_answers WHERE user_id = ?", (viewer.id,))[0]["pts"]
    e.add_field(name="Ты", value=f"{mine} очков", inline=False)
    return e


class QuizAddModal(discord.ui.Modal, title="Новый вопрос дня"):
    question = discord.ui.TextInput(label="Вопрос", style=discord.TextStyle.paragraph, max_length=300)
    correct = discord.ui.TextInput(label="Верный ответ", max_length=80)
    wrong = discord.ui.TextInput(label="Три неверных ответа — каждый с новой строки",
                                 style=discord.TextStyle.paragraph, max_length=300)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wrong = [w.strip() for w in self.wrong.value.splitlines() if w.strip()]
        if len(wrong) != 3:
            await say(interaction, "Нужно ровно три неверных ответа, каждый с новой строки.")
            return
        db.x("INSERT INTO quiz_custom (question, correct, wrong) VALUES (?, ?, ?)",
             (self.question.value.strip(), self.correct.value.strip(), "|".join(wrong)))
        await say(interaction, "✅ Вопрос добавлен в колоду.")


# ================= ПАНЕЛЬ =================

class ColosseumPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Вызвать на дуэль", emoji="⚔️", style=discord.ButtonStyle.danger, custom_id="col:duel")
    async def duel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_legion(interaction.user):
            await say(interaction, "⛔ На арену выходят легионеры.")
            return
        if interaction.user.id in busy:
            await say(interaction, "Ты уже на арене!")
            return
        await say(interaction, "⚔️ Кого вызываешь?", view=ChallengeView())

    @discord.ui.select(placeholder="🏟️ Колизей…", custom_id="col:menu", options=[
        discord.SelectOption(label="Арена — лучшие бойцы", value="arena", emoji="🏆"),
        discord.SelectOption(label="Знатоки — вопрос дня", value="scholars", emoji="🧠"),
        discord.SelectOption(label="Объявить турнир (командование)", value="tournament", emoji="🦁"),
        discord.SelectOption(label="Добавить вопрос дня (командование)", value="addq", emoji="➕"),
    ])
    async def menu(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        choice = select.values[0]
        if choice == "arena":
            await say(interaction, embed=arena_embed(interaction.user))
        elif choice == "scholars":
            await say(interaction, embed=scholars_embed(interaction.user))
        elif choice in ("tournament", "addq"):
            if not can_create(interaction.user):
                await say(interaction, "⛔ Это для Принцепса, Консулов и Сената.")
                return
            await interaction.response.send_modal(TournamentForm() if choice == "tournament" else QuizAddModal())


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🏟️ КОЛИЗЕЙ",
        description="⚔️ Дуэль — бой в закрытой ветке, итог появится здесь.\n"
                    "🧠 Вопрос дня — каждый день в 18:00. 🦁 Турниры и 🏆 Арена — в меню.",
        color=discord.Color.dark_red(),
    )


class Colosseum(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @tasks.loop(minutes=1)
    async def ticker(self) -> None:
        now = time.time()
        # турниры: старт
        for t in db.q("SELECT * FROM tournaments WHERE status = 'signup' AND starts_at <= ?", (int(now),)):
            players = [p["user_id"] for p in db.q("SELECT user_id FROM t_players WHERE tournament_id = ?", (t["id"],))]
            channel = self.bot.get_channel(t["channel_id"])
            if len(players) < 2:
                db.x("UPDATE tournaments SET status = 'cancelled' WHERE id = ?", (t["id"],))
                await refresh_t(self.bot, t["id"])
                if channel:
                    await channel.send(f"🚫 Турнир «{t['name']}» не состоялся — мало бойцов.")
                continue
            try:
                await start_round(self.bot, t["id"], players)
            except Exception:
                log.exception("Старт турнира %s", t["id"])
        # вопрос дня
        try:
            from cogs.operations import local_tz
            local = datetime.now(local_tz())
            today = local.strftime("%Y-%m-%d")
            if db.meta("quiz_on") != "0" and local.hour >= QUIZ_HOUR and db.meta("quiz_day") != today \
                    and colosseum_channel(self.bot):
                db.set_meta("quiz_day", today)
                await post_quiz(self.bot)
        except Exception:
            log.exception("Вопрос дня")

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="колизей", description="Опубликовать и закрепить панель Колизея в этом канале")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await say(interaction, "⛔ Недостаточно прав.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await interaction.channel.send(embed=panel_embed(), view=ColosseumPanel())
        except discord.HTTPException as e:
            await interaction.followup.send(f"Не удалось опубликовать панель: {e.text or e}", ephemeral=True)
            return
        db.set_meta("channel_id", str(interaction.channel.id))
        try:
            await message.pin(reason="Панель Колизея")
            note = "и закреплена"
        except discord.HTTPException:
            note = "но закрепить не вышло — закрепите вручную"
        await interaction.followup.send(f"✅ Панель опубликована {note}. Вопрос дня — каждый день в {QUIZ_HOUR}:00.",
                                        ephemeral=True)

    @app_commands.command(name="вопрос_дня", description="Опубликовать вопрос дня сейчас (командование)")
    @app_commands.guild_only()
    async def quiz_now(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await say(interaction, "⛔ Недостаточно прав.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        err = await post_quiz(self.bot)
        await interaction.followup.send(err or "✅ Вопрос опубликован.", ephemeral=True)

    @app_commands.command(name="дуэль", description="Вызвать легионера на дуэль")
    @app_commands.describe(соперник="Кого вызываешь")
    @app_commands.guild_only()
    async def duel_cmd(self, interaction: discord.Interaction, соперник: discord.Member) -> None:
        if not is_legion(interaction.user):
            await say(interaction, "⛔ На арену выходят легионеры.")
            return
        await challenge(interaction, соперник, edit=False)


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_view(ColosseumPanel())
    bot.add_dynamic_items(TButton, QuizButton)
    await bot.add_cog(Colosseum(bot))
