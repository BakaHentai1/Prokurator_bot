"""Профиль легионера.

/профиль [участник] или долгое нажатие на человека → Приложения → «Профиль».
Собирает в одну карточку: звание, ник в Foxhole, специализацию, часовой пояс,
даты, явку на операции, голосования, выборы, эдикты и склады.

Свой профиль можно дополнить кнопкой «Изменить профиль».
Данные профиля — в отдельном файле data/profiles.db.
"""
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("profiles")

DB_PATH: Path = config.DATA_DIR / "profiles.db"


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id    INTEGER PRIMARY KEY,
                nickname   TEXT NOT NULL DEFAULT '',
                specialty  TEXT NOT NULL DEFAULT '',
                timezone   TEXT NOT NULL DEFAULT '',
                about      TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

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


db: Optional[DB] = None


# ---------- сбор данных из других модулей ----------

def rank_of(member: discord.Member) -> str:
    """Высшее звание Легиона у человека."""
    order = [
        config.PRINCEPS_ROLE_ID, config.SENATE_ROLE_ID, config.CONSUL_ROLE_ID, config.LEGATE_ROLE_ID,
        config.CENTURION_ROLE_ID, config.FABRUM_ROLE_ID, config.MEMBER_ROLE_ID, config.ALLY_ROLE_ID,
        config.WARDEN_ROLE_ID, config.NEWCOMER_ROLE_ID,
    ]
    held = {r.id for r in member.roles}
    for role_id in order:
        if role_id and role_id in held:
            return f"<@&{role_id}>"
    return member.top_role.mention if not member.top_role.is_default() else "—"


def application(user_id: int) -> dict:
    try:
        from cogs.registration import load_data
        return load_data().get(str(user_id), {})
    except Exception:
        return {}


def ops_stats(user_id: int) -> Optional[str]:
    try:
        from cogs import operations
        if operations.db is None:
            return None
        rows = operations.db.conn.execute(
            "SELECT r.answer, COUNT(*) AS n FROM responses r JOIN operations o ON o.id = r.op_id "
            "WHERE r.user_id = ? AND o.status != 'cancelled' GROUP BY r.answer", (user_id,)).fetchall()
        counts = {r["answer"]: r["n"] for r in rows}
        led = operations.db.conn.execute(
            "SELECT COUNT(*) FROM operations WHERE creator_id = ? AND status != 'cancelled'", (user_id,)).fetchone()[0]
    except Exception:
        return None
    text = (f"✅ {counts.get('going', 0)} · ⏰ {counts.get('late', 0)} · ❌ {counts.get('no', 0)}")
    if led:
        text += f"\nКомандовал: {led}"
    return text


def votes_count(user_id: int) -> Optional[int]:
    try:
        from cogs import consulate
        if consulate.db is None:
            return None
        return consulate.db.conn.execute(
            "SELECT COUNT(DISTINCT poll_id) FROM votes WHERE user_id = ?", (user_id,)).fetchone()[0]
    except Exception:
        return None


def elections_stats(user_id: int) -> Optional[str]:
    try:
        from cogs import elections
        if elections.db is None:
            return None
        rows = elections.db.conn.execute(
            "SELECT election_id FROM candidates WHERE user_id = ?", (user_id,)).fetchall()
        won = []
        for r in rows:
            e = elections.db.get(r["election_id"])
            if e and e["phase"] == "done" and user_id in elections.results(e)[1]:
                won.append(e["position"])
        voted = elections.db.conn.execute(
            "SELECT COUNT(DISTINCT election_id) FROM ballots WHERE voter_id = ?", (user_id,)).fetchone()[0]
    except Exception:
        return None
    if not rows and not voted:
        return None
    text = f"Голосовал: {voted} · выдвигался: {len(rows)}"
    if won:
        text += f"\n🏆 Избран: {', '.join(won)}"
    return text


def edicts_count(user_id: int) -> Optional[int]:
    try:
        from cogs import edicts
        if edicts.db is None:
            return None
        return edicts.db.conn.execute("SELECT COUNT(*) FROM acks WHERE user_id = ?", (user_id,)).fetchone()[0]
    except Exception:
        return None


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
        return "—"
    lines = [f"№{w['id']} · {w['name']} — {w['region']}" for w in items[:8]]
    if len(items) > 8:
        lines.append(f"…и ещё {len(items) - 8}")
    return "\n".join(lines)


def iso_ts(value: str) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return None


# ---------- карточка ----------

def profile_embed(member: discord.Member, viewer: discord.abc.User) -> discord.Embed:
    p = db.get(member.id)
    app = application(member.id)
    answers = app.get("fields", {})

    e = discord.Embed(title=f"🪪 {member.display_name}", color=member.color if member.color.value else
                      discord.Color.dark_red())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Звание", value=rank_of(member), inline=True)
    e.add_field(name="Ник в Foxhole", value=p.get("nickname") or app.get("nickname") or "—", inline=True)
    e.add_field(name="Специализация", value=p.get("specialty") or "—", inline=True)
    e.add_field(name="Часовой пояс и онлайн", value=p.get("timezone") or answers.get("Онлайн") or "—", inline=True)

    dates = []
    if member.joined_at:
        dates.append(f"На сервере с <t:{int(member.joined_at.timestamp())}:D>")
    if app.get("status") == "approved":
        ts = iso_ts(app.get("reviewed_at"))
        dates.append(f"Принят {'<t:' + str(ts) + ':D>' if ts else ''}".strip()
                     + (f" · принял <@{app['reviewed_by']}>" if app.get("reviewed_by") else ""))
    if dates:
        e.add_field(name="Служба", value="\n".join(dates), inline=False)

    if p.get("about"):
        e.add_field(name="О себе", value=p["about"], inline=False)

    ops = ops_stats(member.id)
    if ops:
        e.add_field(name="⚔️ Операции (отметки)", value=ops, inline=True)
    votes = votes_count(member.id)
    if votes is not None:
        e.add_field(name="🗳️ Голосований", value=str(votes), inline=True)
    acks = edicts_count(member.id)
    if acks is not None:
        e.add_field(name="📜 Эдиктов прочитано", value=str(acks), inline=True)
    elect = elections_stats(member.id)
    if elect:
        e.add_field(name="🏛️ Выборы", value=elect, inline=False)
    wh = warehouses_text(member.id, viewer)
    if wh is not None:
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


class Profiles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="профиль", description="Профиль легионера")
    @app_commands.describe(участник="Чей профиль показать (по умолчанию — ваш)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, участник: Optional[discord.Member] = None) -> None:
        await show_profile(interaction, участник or interaction.user)


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    await bot.add_cog(Profiles(bot))
    bot.tree.add_command(profile_menu)
