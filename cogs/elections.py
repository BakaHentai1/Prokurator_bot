"""Выборы Легиона.

/выборы (или кнопка на панели Консулата): должность, число мест, срок выдвижения,
срок голосования, условия; затем — кто участвует и роль для победителей.

1. Выдвижение: «Выдвинуться» (с короткой программой), «Снять кандидатуру».
2. Голосование: тайное, можно отметить столько кандидатов, сколько мест.
   Если кандидатов не больше, чем мест, — избираются без голосования.
3. Итоги публикуются сами. Роль выдаётся после «Утвердить» Принцепса (или администратора).
Виды выборов: «Консул» (до 2 мест, кандидаты — только старше легионера),
«Легат» (до 5 мест, выдвигаться могут все легионеры) и «Другие» (свои настройки).
Голосуют все легионеры (или только офицеры — по выбору).

Журнал — в «Архивариум». Данные — в отдельном файле data/elections.db.
"""
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.consulate import AUDIENCES, archive, can_create, has_any, is_admin, legion_roles, officer_roles, parse_duration

log = logging.getLogger("elections")

DB_PATH: Path = config.DATA_DIR / "elections.db"
# Виды выборов: должность, максимум мест, кто может выдвигаться, роль победителям
PRESETS = {
    "consul": {"name": "Консул", "max_seats": 2, "nominees": "senior", "role": lambda: config.CONSUL_ROLE_ID},
    "legate": {"name": "Легат", "max_seats": 5, "nominees": "legion", "role": lambda: config.LEGATE_ROLE_ID},
    "other": {"name": "", "max_seats": 10, "nominees": "legion", "role": lambda: None},
}
NOMINEES = {
    "legion": "все легионеры",
    "senior": "только старше легионера",
}

PHASES = {
    "nominate": "🙋 Выдвижение кандидатов",
    "vote": "🗳️ Тайное голосование",
    "done": "📜 Завершены",
    "cancelled": "🚫 Отменены",
}


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS elections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id      INTEGER NOT NULL,
                message_id      INTEGER,
                position        TEXT    NOT NULL,
                description     TEXT    NOT NULL DEFAULT '',
                seats           INTEGER NOT NULL DEFAULT 1,
                audience        TEXT    NOT NULL DEFAULT 'legion',
                role_id         INTEGER,
                creator_id      INTEGER NOT NULL,
                created_at      INTEGER NOT NULL,
                nominate_ends   INTEGER NOT NULL,
                vote_seconds    INTEGER NOT NULL,
                vote_ends       INTEGER,
                phase           TEXT    NOT NULL DEFAULT 'nominate',
                results_msg_id  INTEGER,
                confirmed_by    INTEGER
            );
            CREATE TABLE IF NOT EXISTS candidates (
                election_id INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                program     TEXT    NOT NULL DEFAULT '',
                ts          INTEGER NOT NULL,
                PRIMARY KEY (election_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS ballots (
                election_id  INTEGER NOT NULL,
                voter_id     INTEGER NOT NULL,
                candidate_id INTEGER NOT NULL,
                ts           INTEGER NOT NULL,
                PRIMARY KEY (election_id, voter_id, candidate_id)
            );
            """
        )
        # Миграция: кто может выдвигаться (у старых выборов — все легионеры)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(elections)")}
        if "nominees" not in cols:
            self.conn.execute("ALTER TABLE elections ADD COLUMN nominees TEXT NOT NULL DEFAULT 'legion'")
        self.conn.commit()

    def create(self, d: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO elections (channel_id, position, description, seats, audience, role_id, creator_id,"
            " created_at, nominate_ends, vote_seconds, nominees) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d["channel_id"], d["position"], d["description"], d["seats"], d["audience"], d["role_id"],
             d["creator_id"], int(time.time()), d["nominate_ends"], d["vote_seconds"], d["nominees"]),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, eid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM elections WHERE id = ?", (eid,)).fetchone()
        return dict(row) if row else None

    def set(self, eid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE elections SET {cols} WHERE id = ?", (*fields.values(), eid))
        self.conn.commit()

    def active(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM elections WHERE phase IN ('nominate', 'vote') AND message_id IS NOT NULL"
        )
        return [dict(r) for r in rows]

    def candidates(self, eid: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM candidates WHERE election_id = ? ORDER BY ts", (eid,))]

    def add_candidate(self, eid: int, user_id: int, name: str, program: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?)",
                          (eid, user_id, name, program, int(time.time())))
        self.conn.commit()

    def remove_candidate(self, eid: int, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM candidates WHERE election_id = ? AND user_id = ?", (eid, user_id))
        self.conn.commit()
        return cur.rowcount > 0

    def cast(self, eid: int, voter_id: int, choices: list[int]) -> bool:
        """Записывает бюллетень. True — если человек голосует впервые."""
        first = self.conn.execute("SELECT 1 FROM ballots WHERE election_id = ? AND voter_id = ?",
                                  (eid, voter_id)).fetchone() is None
        self.conn.execute("DELETE FROM ballots WHERE election_id = ? AND voter_id = ?", (eid, voter_id))
        now = int(time.time())
        for c in choices:
            self.conn.execute("INSERT INTO ballots VALUES (?, ?, ?, ?)", (eid, voter_id, c, now))
        self.conn.commit()
        return first

    def voters(self, eid: int) -> int:
        return self.conn.execute("SELECT COUNT(DISTINCT voter_id) FROM ballots WHERE election_id = ?",
                                 (eid,)).fetchone()[0]

    def tally(self, eid: int) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT candidate_id, COUNT(*) AS n FROM ballots WHERE election_id = ? GROUP BY candidate_id", (eid,))
        return {r["candidate_id"]: r["n"] for r in rows}


db: Optional[DB] = None


# ---------- права ----------

def can_take_part(member: discord.abc.User, e: dict) -> bool:
    if is_admin(member):
        return True
    return has_any(member, officer_roles() if e["audience"] == "officers" else legion_roles())


def senior_roles() -> set[int]:
    """Все звания старше легионера."""
    return legion_roles() - {config.MEMBER_ROLE_ID}


def can_nominate(member: discord.abc.User, e: dict) -> bool:
    if e["nominees"] == "senior":
        return is_admin(member) or has_any(member, senior_roles())
    return can_take_part(member, e)


def can_manage(member: discord.abc.User, e: dict) -> bool:
    return member.id == e["creator_id"] or is_admin(member)


def can_confirm(member: discord.abc.User) -> bool:
    return is_admin(member) or (bool(config.PRINCEPS_ROLE_ID) and has_any(member, {config.PRINCEPS_ROLE_ID}))


# ---------- подсчёт ----------

def results(e: dict) -> tuple[list[tuple[dict, int]], list[int], list[int]]:
    """(кандидаты с голосами по убыванию, избранные, спорные из-за ничьей)."""
    cands = db.candidates(e["id"])
    if e["phase"] == "done" and e["vote_ends"] is None:  # избраны без голосования
        return [(c, 0) for c in cands], [c["user_id"] for c in cands], []
    tally = db.tally(e["id"])
    ranked = sorted(((c, tally.get(c["user_id"], 0)) for c in cands), key=lambda x: -x[1])
    seats = e["seats"]
    if len(ranked) <= seats:
        return ranked, [c["user_id"] for c, n in ranked if n > 0], []
    border = ranked[seats - 1][1]
    above = [c["user_id"] for c, n in ranked if n > border]
    tied = [c["user_id"] for c, n in ranked if n == border]
    if len(above) + len(tied) <= seats:
        return ranked, above + tied, []
    if border == 0:
        return ranked, above, []
    return ranked, above, tied


# ---------- оформление ----------

def post_link(e: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{e['channel_id']}/{e['message_id']}"


def election_embed(e: dict) -> discord.Embed:
    phase = e["phase"]
    color = {"nominate": discord.Color.dark_red(), "vote": discord.Color.dark_red(),
             "done": discord.Color.gold(), "cancelled": discord.Color.dark_grey()}[phase]
    desc = (f"**Мест: {e['seats']}** · Выдвигаться могут: {NOMINEES[e['nominees']]} · "
            f"Голосуют: {AUDIENCES[e['audience']]}")
    if e["role_id"]:
        desc += f" · Роль победителям: <@&{e['role_id']}>"
    if e["description"]:
        desc = e["description"] + "\n\n" + desc
    embed = discord.Embed(title=f"🏛️ ВЫБОРЫ №{e['id']}: {e['position'].upper()}", description=desc, color=color)

    if phase == "nominate":
        embed.add_field(name="Этап", value=f"{PHASES[phase]} до <t:{e['nominate_ends']}:f> (<t:{e['nominate_ends']}:R>)",
                        inline=False)
        hours = e["vote_seconds"] / 3600
        embed.add_field(name="Затем", value=f"🗳️ тайное голосование, {hours:g} ч", inline=False)
    elif phase == "vote":
        embed.add_field(name="Этап", value=f"{PHASES[phase]} до <t:{e['vote_ends']}:f> (<t:{e['vote_ends']}:R>)",
                        inline=False)
        embed.add_field(name="Проголосовало", value=str(db.voters(e["id"])), inline=False)
    else:
        embed.add_field(name="Этап", value=PHASES[phase], inline=False)

    cands = db.candidates(e["id"])
    lines = []
    for i, c in enumerate(cands, 1):
        line = f"**{i}.** <@{c['user_id']}>"
        if c["program"]:
            line += f" — {c['program']}"
        lines.append(line)
    text = "\n".join(lines) or "Пока никто не выдвинулся."
    if len(text) > 1024:
        text = "\n".join(f"**{i}.** <@{c['user_id']}>" for i, c in enumerate(cands, 1))[:1020] + " …"
    embed.add_field(name=f"Кандидаты ({len(cands)})", value=text, inline=False)
    if phase == "vote":
        embed.set_footer(text="Голосование тайное. Бюллетень можно изменить до конца.")
    return embed


def results_embed(e: dict) -> discord.Embed:
    ranked, elected, tied = results(e)
    unopposed = e["vote_ends"] is None
    lines = []
    total = db.voters(e["id"])
    for c, n in ranked:
        mark = "🏆" if c["user_id"] in elected else ("⚖️" if c["user_id"] in tied else "▫️")
        line = f"{mark} <@{c['user_id']}>"
        if not unopposed:
            pct = n / total * 100 if total else 0
            line += f" — {n} ({pct:.0f}%)"
        lines.append(line)
    desc = "\n".join(lines) or "Кандидатов не было."
    if unopposed and elected:
        desc += "\n\nКандидатов не больше, чем мест, — избраны без голосования."
    elif not unopposed:
        desc += f"\n\nПроголосовало: {total}"
    if tied:
        desc += "\n\n⚖️ Ничья за последнее место — решение за Принцепсом."
    embed = discord.Embed(title=f"📜 ИТОГИ ВЫБОРОВ №{e['id']}: {e['position'].upper()}", description=desc[:4000],
                          color=discord.Color.gold())
    if elected:
        embed.add_field(name="Избраны", value=" ".join(f"<@{u}>" for u in elected)[:1024], inline=False)
    if e["confirmed_by"]:
        embed.add_field(name="Утверждено", value=f"<@{e['confirmed_by']}>", inline=False)
    elif e["role_id"] and elected:
        embed.add_field(name="Ожидает утверждения", value="Роль будет выдана после утверждения Принцепсом.",
                        inline=False)
    embed.set_footer(text="Senatus Populusque Legionis")
    return embed


class ElectionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"elec:(?P<eid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, eid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: Optional[int] = None) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, row=row,
                                           custom_id=f"elec:{eid}:{act}"))
        self.eid = eid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["eid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = {
            "nominate": on_nominate, "withdraw": on_withdraw, "startvote": on_start_vote,
            "cancel": on_cancel, "vote": on_vote, "confirm": on_confirm, "swap": on_confirm_swap,
        }.get(self.act)
        if handler:
            await handler(interaction, self.eid)


def election_view(e: dict) -> Optional[discord.ui.View]:
    view = discord.ui.View(timeout=None)
    if e["phase"] == "nominate":
        view.add_item(ElectionButton(e["id"], "nominate", "Выдвинуться", "🙋", discord.ButtonStyle.success, 0))
        view.add_item(ElectionButton(e["id"], "withdraw", "Снять кандидатуру", "🚪", row=0))
        view.add_item(ElectionButton(e["id"], "startvote", "Начать голосование сейчас", "▶️", row=1))
        view.add_item(ElectionButton(e["id"], "cancel", "Отменить выборы", "🚫", discord.ButtonStyle.danger, 1))
    elif e["phase"] == "vote":
        view.add_item(ElectionButton(e["id"], "vote", "Голосовать", "🗳️", discord.ButtonStyle.success, 0))
        view.add_item(ElectionButton(e["id"], "cancel", "Отменить выборы", "🚫", discord.ButtonStyle.danger, 0))
    else:
        return None
    return view


def confirm_view(e: dict) -> Optional[discord.ui.View]:
    _, elected, _ = results(e)
    if not e["role_id"] or not elected or e["confirmed_by"]:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(ElectionButton(e["id"], "confirm", "Утвердить", "✅", discord.ButtonStyle.success))
    view.add_item(ElectionButton(e["id"], "swap", "Утвердить и снять роль с прежних", "🔁", discord.ButtonStyle.primary))
    return view


async def refresh_post(client: discord.Client, e: dict) -> None:
    try:
        channel = client.get_channel(e["channel_id"]) or await client.fetch_channel(e["channel_id"])
        await channel.get_partial_message(e["message_id"]).edit(embed=election_embed(e), view=election_view(e))
    except discord.HTTPException as err:
        log.error("Не удалось обновить пост выборов %s: %s", e["id"], err)


# ---------- этапы ----------

async def start_voting(client: discord.Client, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["phase"] != "nominate":
        return
    cands = db.candidates(eid)
    if not cands:
        db.set(eid, phase="cancelled")
        e = db.get(eid)
        await refresh_post(client, e)
        await archive(client, f"🚫 Выборы №{eid} «{e['position']}» не состоялись: никто не выдвинулся · {post_link(e)}")
        return
    if len(cands) <= e["seats"]:
        db.set(eid, phase="done")
        await finish(client, eid)
        return
    db.set(eid, phase="vote", vote_ends=int(time.time()) + e["vote_seconds"])
    e = db.get(eid)
    await refresh_post(client, e)
    try:
        channel = client.get_channel(e["channel_id"]) or await client.fetch_channel(e["channel_id"])
        ping = " ".join(f"<@&{r}>" for r in (sorted(officer_roles()) if e["audience"] == "officers"
                                              else [config.MEMBER_ROLE_ID]) if r)
        await channel.send(
            (ping + "\n" if ping else "")
            + f"🗳️ Началось тайное голосование на выборах **{e['position']}** — до <t:{e['vote_ends']}:f>. "
            f"{post_link(e)}",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.HTTPException as err:
        log.error("Не удалось объявить голосование выборов %s: %s", eid, err)
    await archive(client, f"🗳️ <t:{int(time.time())}:f> · Выборы №{eid} «{e['position']}»: начато голосование, "
                          f"кандидатов {len(cands)} · {post_link(e)}")


async def finish(client: discord.Client, eid: int) -> None:
    e = db.get(eid)
    if e["phase"] == "vote":
        db.set(eid, phase="done", vote_ends=min(e["vote_ends"], int(time.time())))
    e = db.get(eid)
    await refresh_post(client, e)
    try:
        channel = client.get_channel(e["channel_id"]) or await client.fetch_channel(e["channel_id"])
        msg = await channel.send(embed=results_embed(e), view=confirm_view(e),
                                 reference=channel.get_partial_message(e["message_id"]), mention_author=False,
                                 allowed_mentions=discord.AllowedMentions.none())
        db.set(eid, results_msg_id=msg.id)
    except discord.HTTPException as err:
        log.error("Не удалось опубликовать итоги выборов %s: %s", eid, err)
    await archive(client, f"📜 <t:{int(time.time())}:f> · Итоги выборов №{eid} · {post_link(e)}", results_embed(e))
    log.info("Выборы %s завершены", eid)


# ---------- кнопки ----------

class NominateModal(discord.ui.Modal, title="Выдвижение кандидатуры"):
    program = discord.ui.TextInput(label="Ваша программа (коротко)", style=discord.TextStyle.paragraph,
                                   max_length=300, required=False,
                                   placeholder="Почему вы и что сделаете на этой должности")

    def __init__(self, eid: int) -> None:
        super().__init__()
        self.eid = eid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        e = db.get(self.eid)
        if e is None or e["phase"] != "nominate":
            await interaction.response.send_message("Выдвижение уже закрыто.", ephemeral=True)
            return
        db.add_candidate(self.eid, interaction.user.id, interaction.user.display_name, self.program.value.strip())
        await interaction.response.edit_message(embed=election_embed(e))
        await interaction.followup.send("🙋 Вы выдвинуты кандидатом.", ephemeral=True)
        await archive(interaction.client, f"🙋 <t:{int(time.time())}:f> · {interaction.user.mention} выдвинулся "
                                          f"на выборах №{self.eid} «{e['position']}»")


async def on_nominate(interaction: discord.Interaction, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["phase"] != "nominate":
        await interaction.response.send_message("Выдвижение закрыто.", ephemeral=True)
        return
    if not can_nominate(interaction.user, e):
        who = "звания старше легионера" if e["nominees"] == "senior" else "участники этих выборов"
        await interaction.response.send_message(f"⛔ На эту должность могут выдвигаться только {who}.",
                                                ephemeral=True)
        return
    await interaction.response.send_modal(NominateModal(eid))


async def on_withdraw(interaction: discord.Interaction, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["phase"] != "nominate":
        await interaction.response.send_message("Выдвижение закрыто.", ephemeral=True)
        return
    if not db.remove_candidate(eid, interaction.user.id):
        await interaction.response.send_message("Вы не выдвигались.", ephemeral=True)
        return
    await interaction.response.edit_message(embed=election_embed(e))
    await interaction.followup.send("🚪 Кандидатура снята.", ephemeral=True)
    await archive(interaction.client, f"🚪 <t:{int(time.time())}:f> · {interaction.user.mention} снял кандидатуру "
                                      f"на выборах №{eid}")


async def on_start_vote(interaction: discord.Interaction, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["phase"] != "nominate":
        await interaction.response.send_message("Выдвижение уже закрыто.", ephemeral=True)
        return
    if not can_manage(interaction.user, e):
        await interaction.response.send_message("⛔ Это может только организатор выборов или администратор.",
                                                ephemeral=True)
        return
    await interaction.response.send_message("▶️ Выдвижение закрыто.", ephemeral=True)
    await start_voting(interaction.client, eid)


async def on_cancel(interaction: discord.Interaction, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["phase"] not in ("nominate", "vote"):
        await interaction.response.send_message("Выборы уже завершены.", ephemeral=True)
        return
    if not can_manage(interaction.user, e):
        await interaction.response.send_message("⛔ Это может только организатор выборов или администратор.",
                                                ephemeral=True)
        return
    db.set(eid, phase="cancelled")
    e = db.get(eid)
    await interaction.response.edit_message(embed=election_embed(e), view=None)
    await interaction.followup.send("🚫 Выборы отменены.", ephemeral=True)
    await archive(interaction.client, f"🚫 <t:{int(time.time())}:f> · {interaction.user.mention} отменил выборы "
                                      f"№{eid} «{e['position']}» · {post_link(e)}")


class BallotView(discord.ui.View):
    def __init__(self, e: dict, message: discord.Message) -> None:
        super().__init__(timeout=600)
        self.eid = e["id"]
        self.message = message
        cands = db.candidates(e["id"])[:25]
        self.ballot.options = [
            discord.SelectOption(label=c["name"][:100], value=str(c["user_id"]),
                                 description=(c["program"][:100] or None))
            for c in cands
        ]
        self.ballot.max_values = min(e["seats"], len(cands))
        self.ballot.placeholder = ("Выберите кандидата" if e["seats"] == 1
                                   else f"Выберите до {self.ballot.max_values} кандидатов")

    @discord.ui.select(placeholder="Выберите кандидата", options=[discord.SelectOption(label="—")])
    async def ballot(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        e = db.get(self.eid)
        if e is None or e["phase"] != "vote" or e["vote_ends"] <= time.time():
            await interaction.response.edit_message(content="Голосование уже завершено.", view=None)
            return
        choices = [int(v) for v in select.values]
        first = db.cast(self.eid, interaction.user.id, choices)
        names = ", ".join(f"<@{c}>" for c in choices)
        await interaction.response.edit_message(
            content=f"✅ Бюллетень {'принят' if first else 'изменён'}: {names}\n🔒 Голосование тайное.", view=None
        )
        if first:
            try:
                await self.message.edit(embed=election_embed(e))
            except discord.HTTPException:
                pass


async def on_vote(interaction: discord.Interaction, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["phase"] != "vote" or e["vote_ends"] <= time.time():
        await interaction.response.send_message("Голосование закрыто.", ephemeral=True)
        return
    if not can_take_part(interaction.user, e):
        await interaction.response.send_message("⛔ Голосуют только участники этих выборов.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🗳️ **Выборы: {e['position']}** — тайный бюллетень. Мест: {e['seats']}.",
        view=BallotView(e, interaction.message), ephemeral=True,
    )


async def _confirm(interaction: discord.Interaction, eid: int, swap: bool) -> None:
    e = db.get(eid)
    if e is None or e["phase"] != "done" or e["confirmed_by"]:
        await interaction.response.send_message("Итоги уже утверждены.", ephemeral=True)
        return
    if not can_confirm(interaction.user):
        await interaction.response.send_message("⛔ Утверждает итоги Принцепс.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    role = guild.get_role(e["role_id"]) if e["role_id"] else None
    _, elected, _ = results(e)
    problems = []
    if role is None:
        problems.append("роль не найдена")
    else:
        if swap:
            for m in list(role.members):
                if m.id not in elected:
                    try:
                        await m.remove_roles(role, reason=f"Выборы №{eid}: смена состава")
                    except discord.HTTPException:
                        problems.append(f"не снята роль у {m.mention}")
        for uid in elected:
            member = guild.get_member(uid)
            if member is None:
                problems.append(f"<@{uid}> нет на сервере")
                continue
            try:
                await member.add_roles(role, reason=f"Выборы №{eid}")
            except discord.HTTPException:
                problems.append(f"не выдана роль {member.mention} (роль бота должна быть выше)")
    db.set(eid, confirmed_by=interaction.user.id)
    e = db.get(eid)
    await interaction.message.edit(embed=results_embed(e), view=None)
    text = "✅ Итоги утверждены, роли выданы." if not problems else "⚠️ Утверждено, но: " + "; ".join(problems)
    await interaction.followup.send(text[:2000], ephemeral=True)
    await archive(interaction.client, f"✅ <t:{int(time.time())}:f> · {interaction.user.mention} утвердил итоги "
                                      f"выборов №{eid} «{e['position']}»" + (" (роль снята с прежних)" if swap else ""))


async def on_confirm(interaction: discord.Interaction, eid: int) -> None:
    await _confirm(interaction, eid, swap=False)


async def on_confirm_swap(interaction: discord.Interaction, eid: int) -> None:
    await _confirm(interaction, eid, swap=True)


# ---------- создание ----------

class ElectionTypeView(discord.ui.View):
    """Первый шаг: какие выборы объявляем."""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.select(placeholder="Какие выборы?", options=[
        discord.SelectOption(label="Консул", value="consul", emoji="🏛️",
                             description="До 2 мест · кандидаты — только старше легионера"),
        discord.SelectOption(label="Легат", value="legate", emoji="⚔️",
                             description="До 5 мест · выдвигаться могут все легионеры"),
        discord.SelectOption(label="Другие выборы", value="other", emoji="✏️",
                             description="Своя должность и свои настройки"),
    ])
    async def kind(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        await interaction.response.send_modal(ElectionForm(select.values[0]))


class ElectionForm(discord.ui.Modal, title="Новые выборы"):
    def __init__(self, preset: str = "other") -> None:
        p = PRESETS[preset]
        super().__init__(title=f"Выборы: {p['name']}" if p["name"] else "Новые выборы")
        self.preset = preset
        self.position = discord.ui.TextInput(label="Должность", max_length=60, placeholder="Например: Квестор",
                                             default=p["name"] or None)
        self.seats = discord.ui.TextInput(label=f"Сколько мест (не больше {p['max_seats']})", max_length=2,
                                          default=str(p["max_seats"]) if p["name"] else "1")
        self.nominate = discord.ui.TextInput(label="Выдвижение длится", max_length=20, default="24ч",
                                             placeholder="Например: 12ч, 2д")
        self.vote = discord.ui.TextInput(label="Голосование длится", max_length=20, default="24ч",
                                         placeholder="Например: 12ч, 2д")
        self.description = discord.ui.TextInput(label="Условия и описание (необязательно)",
                                                style=discord.TextStyle.paragraph, max_length=1000, required=False)
        for item in (self.position, self.seats, self.nominate, self.vote, self.description):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        p = PRESETS[self.preset]
        try:
            seats = int(self.seats.value.strip())
        except ValueError:
            seats = 0
        nominate = parse_duration(self.nominate.value)
        vote = parse_duration(self.vote.value)
        if not 1 <= seats <= p["max_seats"] or nominate is None or vote is None:
            await interaction.response.send_message(
                f"Проверьте поля: мест — от 1 до {p['max_seats']}, сроки — например `12ч`, `2д`, `1д 12ч`.",
                ephemeral=True,
            )
            return
        draft = {
            "channel_id": interaction.channel.id,
            "position": self.position.value.strip(),
            "description": self.description.value.strip(),
            "seats": seats,
            "audience": "legion",
            "nominees": p["nominees"],
            "role_id": p["role"]() or None,
            "creator_id": interaction.user.id,
            "nominate_seconds": nominate,
            "vote_seconds": vote,
        }
        view = ElectionSetupView(draft, custom=self.preset == "other")
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class ElectionSetupView(discord.ui.View):
    def __init__(self, draft: dict, custom: bool = True) -> None:
        super().__init__(timeout=900)
        self.draft = draft
        self.done = False
        if not custom:  # у Консула и Легата правило выдвижения фиксировано
            self.remove_item(self.nominees)

    def summary(self) -> str:
        d = self.draft
        role = f"<@&{d['role_id']}>" if d["role_id"] else "не выдаётся"
        return (
            f"**Выборы: {d['position']}** · мест: {d['seats']}\n"
            f"Выдвижение: {d['nominate_seconds'] / 3600:g} ч · голосование: {d['vote_seconds'] / 3600:g} ч\n"
            f"Выдвигаться могут: {NOMINEES[d['nominees']]} · Голосуют: {AUDIENCES[d['audience']]}\n"
            f"Роль победителям: {role}\n\n"
            "Выберите роль для победителей (после утверждения Принцепсом) и нажмите «Объявить выборы»."
        )

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Роль для победителей (необязательно)",
                       min_values=0, max_values=1, row=0)
    async def role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        self.draft["role_id"] = select.values[0].id if select.values else None
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(placeholder="Участвуют: весь Легион",
                       options=[discord.SelectOption(label=label, value=key) for key, label in AUDIENCES.items()],
                       row=1)
    async def audience(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["audience"] = select.values[0]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(placeholder="Кто может выдвигаться: все легионеры",
                       options=[discord.SelectOption(label=f"Выдвигаться могут {label}", value=key)
                                for key, label in NOMINEES.items()], row=2)
    async def nominees(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["nominees"] = select.values[0]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Объявить выборы", emoji="🏛️", style=discord.ButtonStyle.success, row=3)
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
        eid = db.create(dict(d, nominate_ends=int(time.time()) + d["nominate_seconds"]))
        e = db.get(eid)
        ping_roles = sorted(officer_roles()) if d["audience"] == "officers" else [config.MEMBER_ROLE_ID]
        ping = " ".join(f"<@&{r}>" for r in ping_roles if r) or None
        try:
            message = await interaction.channel.send(content=ping, embed=election_embed(e), view=election_view(e),
                                                     allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException as err:
            db.set(eid, phase="cancelled")
            await interaction.edit_original_response(content=f"Не удалось опубликовать: {err.text or err}")
            return
        db.set(eid, message_id=message.id)
        e = db.get(eid)
        await interaction.edit_original_response(content=f"✅ Выборы №{eid} объявлены: {message.jump_url}")
        await archive(interaction.client,
                      f"🏛️ <t:{int(time.time())}:f> · {interaction.user.mention} объявил выборы №{eid} "
                      f"«{e['position']}», мест: {e['seats']}, выдвижение до <t:{e['nominate_ends']}:f> · "
                      f"{message.jump_url}")


class Elections(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @tasks.loop(minutes=1)
    async def ticker(self) -> None:
        now = time.time()
        for e in db.active():
            try:
                if e["phase"] == "nominate" and now >= e["nominate_ends"]:
                    await start_voting(self.bot, e["id"])
                elif e["phase"] == "vote" and now >= e["vote_ends"]:
                    await finish(self.bot, e["id"])
            except Exception:
                log.exception("Ошибка при обработке выборов %s", e["id"])

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="выборы", description="Объявить выборы в этом канале")
    @app_commands.guild_only()
    async def new_election(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await interaction.response.send_message("⛔ Выборы объявляют Принцепс, Консулы и Сенат.", ephemeral=True)
            return
        await interaction.response.send_message("Какие выборы объявляем?", view=ElectionTypeView(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_dynamic_items(ElectionButton)
    await bot.add_cog(Elections(bot))
