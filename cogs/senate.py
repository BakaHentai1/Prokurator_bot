"""Сенат: закрытые инструменты офицеров (Центурион и выше).

- Чёрный список: ник в Foxhole и/или Discord, причина. Бот предупреждает офицеров,
  если человек из списка подаёт анкету или заходит на сервер.
- Личное дело: заметки офицеров (видны только офицерам) и выговоры (человек получает
  уведомление в личку). Открывается кнопкой на панели, командой /дело или
  долгим нажатием на человека → Приложения → «Личное дело».
- /сенат — закреплённая панель.

Права:
  смотреть дела, список и выговоры — все офицеры (Центурион и выше);
  выносить выговоры — Принцепс, Консулы, Легаты;
  пополнять чёрный список — Принцепс, Консулы, Легаты, Центурионы;
  заметки, снятие выговоров, удаление из чёрного списка — Принцепс и Консулы.

Представления к повышению: офицер предлагает одного или нескольких кандидатов в звание,
офицеры голосуют «За»/«Против», Принцепс утверждает (всех или часть). Бот меняет роли
и публикует приказ в «Консулате».
Данные — в отдельном файле data/senate.db.
"""
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.consulate import can_create, has_any, is_admin, officer_roles

log = logging.getLogger("senate")

DB_PATH: Path = config.DATA_DIR / "senate.db"
WARN_ALERT = 3  # после стольких действующих выговоров бот обращает внимание офицеров


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                author_id  INTEGER NOT NULL,
                kind       TEXT    NOT NULL,          -- note / warning
                text       TEXT    NOT NULL,
                ts         INTEGER NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1,
                removed_by INTEGER,
                removed_ts INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_notes_user ON notes (user_id);
            CREATE TABLE IF NOT EXISTS promotions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER,
                rank_id     INTEGER NOT NULL,
                reason      TEXT    NOT NULL,
                author_id   INTEGER NOT NULL,
                ts          INTEGER NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open',   -- open / approved / rejected
                decided_by  INTEGER,
                decided_ts  INTEGER
            );
            CREATE TABLE IF NOT EXISTS promo_candidates (
                promo_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                result   TEXT,                                 -- approved / rejected
                PRIMARY KEY (promo_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS promo_votes (
                promo_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                vote     INTEGER NOT NULL,                     -- 1 за / 0 против
                ts       INTEGER NOT NULL,
                PRIMARY KEY (promo_id, voter_id)
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER,
                nickname   TEXT    NOT NULL DEFAULT '',
                reason     TEXT    NOT NULL,
                author_id  INTEGER NOT NULL,
                ts         INTEGER NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        self.conn.commit()

    # личное дело
    def add_note(self, user_id: int, author_id: int, kind: str, text: str) -> int:
        cur = self.conn.execute("INSERT INTO notes (user_id, author_id, kind, text, ts) VALUES (?, ?, ?, ?, ?)",
                                (user_id, author_id, kind, text, int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def notes(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY ts DESC", (user_id,))]

    def get_note(self, nid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
        return dict(row) if row else None

    def deactivate(self, nid: int, by_id: int) -> None:
        self.conn.execute("UPDATE notes SET active = 0, removed_by = ?, removed_ts = ? WHERE id = ?",
                          (by_id, int(time.time()), nid))
        self.conn.commit()

    def active_warnings(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM notes WHERE user_id = ? AND kind = 'warning' AND active = 1 ORDER BY ts", (user_id,))]

    # чёрный список
    def bl_add(self, discord_id: Optional[int], nickname: str, reason: str, author_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO blacklist (discord_id, nickname, reason, author_id, ts) VALUES (?, ?, ?, ?, ?)",
            (discord_id, nickname, reason, author_id, int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def bl_all(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM blacklist WHERE active = 1 ORDER BY ts DESC")]

    def bl_get(self, bid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM blacklist WHERE id = ?", (bid,)).fetchone()
        return dict(row) if row else None

    def bl_remove(self, bid: int) -> None:
        self.conn.execute("UPDATE blacklist SET active = 0 WHERE id = ?", (bid,))
        self.conn.commit()

    # представления
    def promo_create(self, channel_id: int, rank_id: int, reason: str, author_id: int, candidates: list[int]) -> int:
        cur = self.conn.execute(
            "INSERT INTO promotions (channel_id, rank_id, reason, author_id, ts) VALUES (?, ?, ?, ?, ?)",
            (channel_id, rank_id, reason, author_id, int(time.time())))
        pid = cur.lastrowid
        for uid in candidates:
            self.conn.execute("INSERT OR IGNORE INTO promo_candidates (promo_id, user_id) VALUES (?, ?)", (pid, uid))
        self.conn.commit()
        return pid

    def promo(self, pid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM promotions WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None

    def promo_set(self, pid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE promotions SET {cols} WHERE id = ?", (*fields.values(), pid))
        self.conn.commit()

    def promo_candidates(self, pid: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM promo_candidates WHERE promo_id = ?", (pid,))]

    def promo_result(self, pid: int, user_id: int, result: str) -> None:
        self.conn.execute("UPDATE promo_candidates SET result = ? WHERE promo_id = ? AND user_id = ?",
                          (result, pid, user_id))
        self.conn.commit()

    def promo_vote(self, pid: int, voter_id: int, vote: int) -> Optional[int]:
        row = self.conn.execute("SELECT vote FROM promo_votes WHERE promo_id = ? AND voter_id = ?",
                                (pid, voter_id)).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO promo_votes VALUES (?, ?, ?, ?)",
                          (pid, voter_id, vote, int(time.time())))
        self.conn.commit()
        return row["vote"] if row else None

    def promo_votes(self, pid: int) -> tuple[list[int], list[int]]:
        rows = self.conn.execute("SELECT voter_id, vote FROM promo_votes WHERE promo_id = ? ORDER BY ts", (pid,))
        yes, no = [], []
        for r in rows:
            (yes if r["vote"] else no).append(r["voter_id"])
        return yes, no

    def open_promos(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM promotions WHERE status = 'open' AND message_id IS NOT NULL ORDER BY ts")]


db: Optional[DB] = None


# ---------- права ----------

def _role(member: discord.abc.User, role_id: int) -> bool:
    return bool(role_id) and has_any(member, {role_id})


def is_officer(member: discord.abc.User) -> bool:
    """Смотреть: Центурион и выше (и администраторы)."""
    return is_admin(member) or has_any(member, officer_roles())


def is_command(member: discord.abc.User) -> bool:
    """Принцепс и Консулы (и администраторы)."""
    return is_admin(member) or _role(member, config.PRINCEPS_ROLE_ID) or _role(member, config.CONSUL_ROLE_ID)


def can_warn(member: discord.abc.User) -> bool:
    return is_command(member) or _role(member, config.LEGATE_ROLE_ID)


def can_blacklist(member: discord.abc.User) -> bool:
    return can_warn(member) or _role(member, config.CENTURION_ROLE_ID)


async def no_rights(interaction: discord.Interaction, who: str) -> None:
    text = f"⛔ Это могут только {who}."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def deny(interaction: discord.Interaction) -> None:
    text = "⛔ Это доступно только офицерам Сената (Центурион и выше)."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ---------- чёрный список ----------

def normalize(nick: str) -> str:
    """Ник без тегов полка в скобках, пробелов и знаков: «[KGB] Spy_Master [7th]» → «spymaster»."""
    nick = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", "", nick)
    return re.sub(r"[\s_\-.|]", "", nick).casefold()


def blacklist_matches(user_id: Optional[int] = None, nickname: str = "") -> list[dict]:
    """Записи чёрного списка, совпадающие с аккаунтом или ником."""
    if db is None:
        return []
    nick = normalize(nickname) if nickname else ""
    found = []
    for b in db.bl_all():
        if user_id and b["discord_id"] == user_id:
            found.append(b)
        elif nick and b["nickname"]:
            listed = normalize(b["nickname"])
            # совпадение с учётом тегов полка: «[KGB] SpyMaster» ↔ «SpyMaster»
            if listed and (listed == nick or (len(listed) >= 4 and listed in nick)
                           or (len(nick) >= 4 and nick in listed)):
                found.append(b)
    return found


def bl_line(b: dict) -> str:
    who = []
    if b["nickname"]:
        who.append(f"**{b['nickname']}**")
    if b["discord_id"]:
        who.append(f"<@{b['discord_id']}> (`{b['discord_id']}`)")
    return f"№{b['id']} · {' · '.join(who) or '—'} — {b['reason']}"


async def senate_channel(client: discord.Client) -> Optional[discord.abc.Messageable]:
    if not config.SENATE_CHANNEL_ID:
        return None
    try:
        return client.get_channel(config.SENATE_CHANNEL_ID) or await client.fetch_channel(config.SENATE_CHANNEL_ID)
    except discord.HTTPException:
        return None


async def alert_application(client: discord.Client, user: discord.abc.User, nickname: str,
                            form_message: discord.Message) -> None:
    """Вызывается из регистрации: анкета от человека из чёрного списка."""
    matches = blacklist_matches(user.id, nickname)
    if not matches:
        return
    text = (f"⛔ **ВНИМАНИЕ: чёрный список!** Анкету подал {user.mention} (ник «{nickname}»).\n"
            + "\n".join(bl_line(b) for b in matches[:5]))
    try:
        await form_message.reply(text[:2000], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    channel = await senate_channel(client)
    if channel and channel.id != form_message.channel.id:
        try:
            await channel.send(text[:1900] + f"\n{form_message.jump_url}", allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


class BlacklistAddModal(discord.ui.Modal, title="Добавить в чёрный список"):
    nickname = discord.ui.TextInput(label="Ник в Foxhole", max_length=64, required=False)
    discord_id = discord.ui.TextInput(label="Discord: ID или упоминание (необязательно)", max_length=40,
                                      required=False, placeholder="Например: 123456789012345678")
    reason = discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, user: Optional[discord.abc.User] = None) -> None:
        super().__init__()
        if user is not None:
            self.discord_id.default = str(user.id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_blacklist(interaction.user):
            await no_rights(interaction, "Принцепс, Консулы, Легаты и Центурионы")
            return
        raw = re.sub(r"\D", "", self.discord_id.value)
        did = int(raw) if len(raw) >= 15 else None
        nick = self.nickname.value.strip()
        if not did and not nick:
            await interaction.response.send_message("Укажите ник в Foxhole или Discord ID.", ephemeral=True)
            return
        bid = db.bl_add(did, nick, self.reason.value.strip(), interaction.user.id)
        await interaction.response.send_message(f"⛔ Добавлено в чёрный список: {bl_line(db.bl_get(bid))}",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        log.info("Чёрный список: добавлена запись %s (%s)", bid, interaction.user)


class BlacklistRemoveView(discord.ui.View):
    def __init__(self, items: list[dict]) -> None:
        super().__init__(timeout=300)
        self.pick.options = [
            discord.SelectOption(label=(f"№{b['id']} · {b['nickname'] or b['discord_id']}")[:100],
                                 description=b["reason"][:100], value=str(b["id"]))
            for b in items[:25]
        ]

    @discord.ui.select(placeholder="Убрать из чёрного списка…", options=[discord.SelectOption(label="—")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        b = db.bl_get(int(select.values[0]))
        if b is None:
            return
        if not is_command(interaction.user):
            await no_rights(interaction, "Принцепс и Консулы")
            return
        db.bl_remove(b["id"])
        await interaction.response.edit_message(content=f"✅ Запись №{b['id']} убрана из чёрного списка.",
                                                embed=None, view=None)


def blacklist_embed() -> discord.Embed:
    items = db.bl_all()
    text = "\n".join(bl_line(b) for b in items[:30]) or "Список пуст."
    if len(items) > 30:
        text += f"\n…и ещё {len(items) - 30}"
    return discord.Embed(title=f"⛔ Чёрный список: {len(items)}", description=text[:4000],
                         color=discord.Color.darker_grey())


class BlacklistView(discord.ui.View):
    def __init__(self, viewer: discord.abc.User) -> None:
        super().__init__(timeout=300)
        if not can_blacklist(viewer):
            self.remove_item(self.add)
        if not is_command(viewer):
            self.remove_item(self.remove)

    @discord.ui.button(label="Добавить", emoji="➕", style=discord.ButtonStyle.danger)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BlacklistAddModal())

    @discord.ui.button(label="Убрать запись", emoji="➖", style=discord.ButtonStyle.secondary)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        items = db.bl_all()
        if not items:
            await interaction.response.send_message("Список пуст.", ephemeral=True)
            return
        await interaction.response.send_message("Какую запись убрать?", view=BlacklistRemoveView(items),
                                                ephemeral=True)


# ---------- личное дело ----------

def dossier_embed(member: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=f"📂 Личное дело · {member.display_name}", color=discord.Color.dark_gold())
    e.set_thumbnail(url=member.display_avatar.url)
    try:
        from cogs import profiles
        if isinstance(member, discord.Member) and profiles.db is not None:
            e.add_field(name="Звание", value=profiles.rank_of(member), inline=True)
            app = profiles.application(member.id)
            p = profiles.db.get(member.id)
            e.add_field(name="Ник в Foxhole", value=p.get("nickname") or app.get("nickname") or "—", inline=True)
            vac = profiles.db.vacation(member.id)
            if vac:
                e.add_field(name="Статус", value=f"🏖️ брейквар до <t:{vac['until']}:D>", inline=True)
    except Exception:
        log.exception("Личное дело: профиль")
    if isinstance(member, discord.Member) and member.joined_at:
        e.add_field(name="На сервере с", value=f"<t:{int(member.joined_at.timestamp())}:D>", inline=True)

    warnings = db.active_warnings(member.id)
    mark = " ⚠️" if len(warnings) >= WARN_ALERT else ""
    e.add_field(name="Действующих выговоров", value=f"**{len(warnings)}**{mark}", inline=True)

    matches = blacklist_matches(member.id)
    if matches:
        e.add_field(name="⛔ Чёрный список", value="\n".join(bl_line(b) for b in matches)[:1024], inline=False)

    lines = []
    for n in db.notes(member.id)[:12]:
        icon = "⚠️ Выговор" if n["kind"] == "warning" else "📝"
        line = f"{icon} №{n['id']} · <t:{n['ts']}:d> · <@{n['author_id']}>: {n['text']}"
        if not n["active"]:
            line = f"~~{line}~~ (снят)" if n["kind"] == "warning" else f"~~{line}~~"
        lines.append(line)
    text = "\n".join(lines) or "Записей нет."
    if len(text) > 1024:
        text = text[:1010].rsplit("\n", 1)[0] + "\n…"
    e.add_field(name="Записи офицеров", value=text, inline=False)
    e.set_footer(text="Видно только офицерам Сената")
    return e


class NoteModal(discord.ui.Modal):
    def __init__(self, member: discord.abc.User, kind: str) -> None:
        super().__init__(title=("Выговор: " if kind == "warning" else "Заметка: ") + member.display_name[:30])
        self.member = member
        self.kind = kind
        label = "Причина (человек увидит её в личке)" if kind == "warning" else "Заметка (видна только офицерам)"
        self.text = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.kind == "warning" and not can_warn(interaction.user):
            await no_rights(interaction, "Принцепс, Консулы и Легаты")
            return
        if self.kind == "note" and not is_command(interaction.user):
            await no_rights(interaction, "Принцепс и Консулы")
            return
        nid = db.add_note(self.member.id, interaction.user.id, self.kind, self.text.value.strip())
        extra = ""
        if self.kind == "warning":
            try:
                await self.member.send(
                    f"⚠️ **Выговор от командования Легиона**\nПричина: {self.text.value.strip()}\n\n"
                    "*Senatus Populusque Legionis*")
            except discord.HTTPException:
                extra = "\n(личка закрыта — уведомление не доставлено)"
            count = len(db.active_warnings(self.member.id))
            if count >= WARN_ALERT:
                extra += f"\n⚠️ У {self.member.mention} уже **{count}** действующих выговоров."
        await interaction.response.edit_message(embed=dossier_embed(self.member),
                                                view=DossierView(self.member, interaction.user))
        await interaction.followup.send(
            ("⚠️ Выговор вынесен" if self.kind == "warning" else "📝 Заметка добавлена") + f" (№{nid})." + extra,
            ephemeral=True)
        log.info("Личное дело %s: %s №%s (%s)", self.member.id, self.kind, nid, interaction.user)


class RemoveWarningView(discord.ui.View):
    def __init__(self, member: discord.abc.User, warnings: list[dict]) -> None:
        super().__init__(timeout=300)
        self.member = member
        self.pick.options = [
            discord.SelectOption(label=f"№{w['id']} · {w['text'][:90]}", value=str(w["id"]))
            for w in warnings[:25]
        ]

    @discord.ui.select(placeholder="Какой выговор снять?", options=[discord.SelectOption(label="—")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        w = db.get_note(int(select.values[0]))
        if w is None or not w["active"]:
            await interaction.response.edit_message(content="Выговор уже снят.", view=None)
            return
        if not is_command(interaction.user):
            await no_rights(interaction, "Принцепс и Консулы")
            return
        db.deactivate(w["id"], interaction.user.id)
        await interaction.response.edit_message(content=f"✅ Выговор №{w['id']} снят.", view=None)
        try:
            await self.member.send(f"✅ Выговор снят: {w['text']}\n\n*Senatus Populusque Legionis*")
        except discord.HTTPException:
            pass


class DossierView(discord.ui.View):
    """Кнопки показываются по правам того, кто открыл дело."""

    def __init__(self, member: discord.abc.User, viewer: discord.abc.User) -> None:
        super().__init__(timeout=900)
        self.member = member
        if not is_command(viewer):
            self.remove_item(self.note)
            self.remove_item(self.unwarn)
        if not can_warn(viewer):
            self.remove_item(self.warning)
        if not can_blacklist(viewer):
            self.remove_item(self.blacklist)

    @discord.ui.button(label="Заметка", emoji="📝", style=discord.ButtonStyle.secondary)
    async def note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_command(interaction.user):
            await no_rights(interaction, "Принцепс и Консулы")
            return
        await interaction.response.send_modal(NoteModal(self.member, "note"))

    @discord.ui.button(label="Выговор", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def warning(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not can_warn(interaction.user):
            await no_rights(interaction, "Принцепс, Консулы и Легаты")
            return
        await interaction.response.send_modal(NoteModal(self.member, "warning"))

    @discord.ui.button(label="Снять выговор", emoji="✅", style=discord.ButtonStyle.secondary)
    async def unwarn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_command(interaction.user):
            await no_rights(interaction, "Принцепс и Консулы")
            return
        warnings = db.active_warnings(self.member.id)
        if not warnings:
            await interaction.response.send_message("Действующих выговоров нет.", ephemeral=True)
            return
        await interaction.response.send_message("Выберите выговор:", view=RemoveWarningView(self.member, warnings),
                                                ephemeral=True)

    @discord.ui.button(label="В чёрный список", emoji="⛔", style=discord.ButtonStyle.secondary)
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not can_blacklist(interaction.user):
            await no_rights(interaction, "Принцепс, Консулы, Легаты и Центурионы")
            return
        await interaction.response.send_modal(BlacklistAddModal(self.member))


async def show_dossier(interaction: discord.Interaction, member: discord.abc.User) -> None:
    if not is_officer(interaction.user):
        await deny(interaction)
        return
    if member.bot:
        await interaction.response.send_message("У ботов нет личного дела.", ephemeral=True)
        return
    view = DossierView(member, interaction.user)
    await interaction.response.send_message(embed=dossier_embed(member), view=view if view.children else None,
                                            ephemeral=True)


@app_commands.context_menu(name="Личное дело")
@app_commands.guild_only()
async def dossier_menu(interaction: discord.Interaction, member: discord.Member) -> None:
    await show_dossier(interaction, member)


class PickMemberView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Чьё личное дело открыть?")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await show_dossier(interaction, select.values[0])


# ---------- представления к повышению ----------

def is_princeps(member: discord.abc.User) -> bool:
    return is_admin(member) or _role(member, config.PRINCEPS_ROLE_ID)


def rank_ladder() -> list[int]:
    """Звания, в которые можно представить (снизу вверх)."""
    return [r for r in (config.FABRUM_ROLE_ID, config.CENTURION_ROLE_ID, config.LEGATE_ROLE_ID,
                        config.CONSUL_ROLE_ID, config.SENATE_ROLE_ID) if r]


def promo_link(p: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{p['channel_id']}/{p['message_id']}"


def promo_embed(p: dict) -> discord.Embed:
    status = p["status"]
    color = {"open": discord.Color.dark_gold(), "approved": discord.Color.green(),
             "rejected": discord.Color.dark_grey()}[status]
    title = f"🎖️ ПРЕДСТАВЛЕНИЕ №{p['id']} К ЗВАНИЮ"
    if status == "approved":
        title += " · УТВЕРЖДЕНО"
    elif status == "rejected":
        title += " · ОТКЛОНЕНО"
    e = discord.Embed(title=title, color=color)
    e.add_field(name="Звание", value=f"<@&{p['rank_id']}>", inline=True)
    e.add_field(name="Представил", value=f"<@{p['author_id']}>", inline=True)
    cands = db.promo_candidates(p["id"])
    lines = []
    for c in cands:
        mark = {"approved": "✅ ", "rejected": "❌ "}.get(c["result"] or "", "")
        lines.append(f"{mark}<@{c['user_id']}>")
    e.add_field(name=f"Кандидаты ({len(cands)})", value="\n".join(lines)[:1024] or "—", inline=False)
    e.add_field(name="Обоснование", value=p["reason"][:1024], inline=False)
    yes, no = db.promo_votes(p["id"])
    e.add_field(name=f"👍 За ({len(yes)})", value=(" ".join(f"<@{u}>" for u in yes) or "—")[:1024], inline=True)
    e.add_field(name=f"👎 Против ({len(no)})", value=(" ".join(f"<@{u}>" for u in no) or "—")[:1024], inline=True)
    if p["decided_by"]:
        e.add_field(name="Решение", value=f"<@{p['decided_by']}> · <t:{p['decided_ts']}:f>", inline=False)
    elif status == "open":
        e.set_footer(text="Голосуют офицеры. Утверждает Принцепс.")
    return e


class PromoButton(discord.ui.DynamicItem[discord.ui.Button], template=r"promo:(?P<pid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, pid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: Optional[int] = None) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, row=row,
                                           custom_id=f"promo:{pid}:{act}"))
        self.pid = pid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["pid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.act in ("yes", "no"):
            await promo_vote(interaction, self.pid, self.act == "yes")
        elif self.act == "decide":
            await promo_decide(interaction, self.pid)


def promo_view(p: dict) -> Optional[discord.ui.View]:
    if p["status"] != "open":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(PromoButton(p["id"], "yes", "За", "👍", discord.ButtonStyle.success))
    view.add_item(PromoButton(p["id"], "no", "Против", "👎", discord.ButtonStyle.danger))
    view.add_item(PromoButton(p["id"], "decide", "Решение Принцепса", "⚖️", discord.ButtonStyle.primary))
    return view


async def promo_vote(interaction: discord.Interaction, pid: int, yes: bool) -> None:
    p = db.promo(pid)
    if p is None or p["status"] != "open":
        await interaction.response.send_message("Представление уже рассмотрено.", ephemeral=True)
        return
    if not is_officer(interaction.user):
        await deny(interaction)
        return
    if interaction.user.id in {c["user_id"] for c in db.promo_candidates(pid)}:
        await interaction.response.send_message("Кандидаты не голосуют за своё представление.", ephemeral=True)
        return
    db.promo_vote(pid, interaction.user.id, 1 if yes else 0)
    await interaction.response.edit_message(embed=promo_embed(db.promo(pid)))


class PromoDecisionView(discord.ui.View):
    """Принцепс отмечает, кого утвердить (по умолчанию — всех)."""

    def __init__(self, p: dict, guild: discord.Guild, message: discord.Message) -> None:
        super().__init__(timeout=600)
        self.pid = p["id"]
        self.message = message
        self.remove_old = True
        cands = db.promo_candidates(p["id"])
        self.chosen = {c["user_id"] for c in cands}
        options = []
        for c in cands[:25]:
            m = guild.get_member(c["user_id"])
            options.append(discord.SelectOption(label=(m.display_name if m else str(c["user_id"]))[:100],
                                                value=str(c["user_id"]), default=True))
        self.pick.options = options
        self.pick.max_values = len(options)

    @discord.ui.select(placeholder="Кого утвердить", min_values=0, options=[discord.SelectOption(label="—")], row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.chosen = {int(v) for v in select.values}
        for o in select.options:
            o.default = o.value in select.values
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(placeholder="Прежние звания: снять", options=[
        discord.SelectOption(label="Снять прежние звания (Фабрум, Центурион…)", value="1", emoji="🔁", default=True),
        discord.SelectOption(label="Оставить прежние звания", value="0", emoji="➕"),
    ], row=1)
    async def old(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.remove_old = select.values[0] == "1"
        for o in select.options:
            o.default = o.value == select.values[0]
        await interaction.response.edit_message(content=self.summary(), view=self)

    def summary(self) -> str:
        names = " ".join(f"<@{u}>" for u in sorted(self.chosen)) or "никого"
        return (f"⚖️ **Решение по представлению №{self.pid}**\nУтвердить: {names}\n"
                f"Прежние звания: {'снять' if self.remove_old else 'оставить'}.\n"
                "Остальные кандидаты будут отклонены.")[:2000]

    @discord.ui.button(label="Утвердить выбранных", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, approve=True)

    @discord.ui.button(label="Отклонить всех", emoji="❌", style=discord.ButtonStyle.danger, row=2)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, approve=False)

    async def _finish(self, interaction: discord.Interaction, approve: bool) -> None:
        p = db.promo(self.pid)
        if p is None or p["status"] != "open":
            await interaction.response.edit_message(content="Представление уже рассмотрено.", view=None)
            return
        if not is_princeps(interaction.user):
            await no_rights(interaction, "Принцепс")
            return
        self.stop()
        await interaction.response.edit_message(content="⏳ Выполняю…", view=None)
        guild = interaction.guild
        rank = guild.get_role(p["rank_id"])
        approved = sorted(self.chosen) if approve else []
        problems = []
        ladder = set(rank_ladder())
        for c in db.promo_candidates(self.pid):
            uid = c["user_id"]
            if uid not in approved:
                db.promo_result(self.pid, uid, "rejected")
                continue
            member = guild.get_member(uid)
            if member is None or rank is None:
                problems.append(f"<@{uid}>: нет на сервере или звание не найдено")
                db.promo_result(self.pid, uid, "rejected")
                continue
            try:
                await member.add_roles(rank, reason=f"Представление №{self.pid}")
                if self.remove_old:
                    old = [r for r in member.roles if r.id in ladder and r.id != rank.id]
                    if old:
                        await member.remove_roles(*old, reason=f"Представление №{self.pid}: повышение")
                db.promo_result(self.pid, uid, "approved")
            except discord.HTTPException:
                problems.append(f"{member.mention}: не хватает прав (роль бота должна быть выше)")
                db.promo_result(self.pid, uid, "rejected")
        done = [c["user_id"] for c in db.promo_candidates(self.pid) if c["result"] == "approved"]
        db.promo_set(self.pid, status="approved" if done else "rejected", decided_by=interaction.user.id,
                     decided_ts=int(time.time()))
        p = db.promo(self.pid)
        try:
            await self.message.edit(embed=promo_embed(p), view=None)
        except discord.HTTPException:
            pass

        if done:
            order = (f"📜 **ПРИКАЗ №{self.pid}**\n\nИменем Сената и Принцепса "
                     f"{', '.join(f'<@{u}>' for u in done)} {'возводятся' if len(done) > 1 else 'возводится'} "
                     f"в звание <@&{p['rank_id']}>.\n\nЗа: {p['reason'][:500]}\n\n*Senatus Populusque Legionis*")
            target = config.CONSULATE_CHANNEL_ID or config.LEGION_CHAT_CHANNEL_ID
            if target:
                try:
                    channel = interaction.client.get_channel(target) or await interaction.client.fetch_channel(target)
                    await channel.send(order[:2000], allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    problems.append("приказ не отправлен: проверьте права бота в «Консулате»")
        from cogs.consulate import archive
        await archive(interaction.client,
                      f"🎖️ <t:{int(time.time())}:f> · {interaction.user.mention} рассмотрел представление №{self.pid} "
                      f"к званию <@&{p['rank_id']}>: утверждены {', '.join(f'<@{u}>' for u in done) or 'никто'} · "
                      f"{promo_link(p)}")
        text = f"✅ Готово: утверждено {len(done)}." if done else "❌ Представление отклонено."
        if problems:
            text += "\n⚠️ " + "; ".join(problems)
        await interaction.edit_original_response(content=text[:2000])


async def promo_decide(interaction: discord.Interaction, pid: int) -> None:
    p = db.promo(pid)
    if p is None or p["status"] != "open":
        await interaction.response.send_message("Представление уже рассмотрено.", ephemeral=True)
        return
    if not is_princeps(interaction.user):
        await no_rights(interaction, "Принцепс")
        return
    view = PromoDecisionView(p, interaction.guild, interaction.message)
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class PromoReasonModal(discord.ui.Modal, title="Представление к повышению"):
    reason = discord.ui.TextInput(label="Обоснование", style=discord.TextStyle.paragraph, max_length=1000,
                                  placeholder="За какие заслуги, чем отличились")

    def __init__(self, setup: "PromoSetupView") -> None:
        super().__init__()
        self.setup = setup

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        st = self.setup
        if st.done:
            return
        st.done = True
        st.stop()
        await interaction.response.edit_message(content="⏳ Публикую…", view=None)
        pid = db.promo_create(interaction.channel.id, st.rank_id, self.reason.value.strip(), interaction.user.id,
                              sorted(st.candidates))
        p = db.promo(pid)
        try:
            message = await interaction.channel.send(embed=promo_embed(p), view=promo_view(p),
                                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            db.promo_set(pid, status="rejected")
            await interaction.edit_original_response(content=f"Не удалось опубликовать: {e.text or e}")
            return
        db.promo_set(pid, message_id=message.id)
        await interaction.edit_original_response(content=f"✅ Представление №{pid} опубликовано: {message.jump_url}")


class PromoSetupView(discord.ui.View):
    def __init__(self, guild: discord.Guild) -> None:
        super().__init__(timeout=900)
        self.candidates: set[int] = set()
        self.rank_id: Optional[int] = None
        self.done = False
        options = []
        for rid in rank_ladder():
            role = guild.get_role(rid)
            if role:
                options.append(discord.SelectOption(label=role.name[:100], value=str(rid)))
        if options:
            self.rank.options = options
        else:
            self.remove_item(self.rank)

    def summary(self) -> str:
        who = " ".join(f"<@{u}>" for u in sorted(self.candidates)) or "не выбраны"
        rank = f"<@&{self.rank_id}>" if self.rank_id else "не выбрано"
        return (f"🎖️ **Представление к повышению**\nКандидаты: {who}\nЗвание: {rank}\n\n"
                "Выберите одного или нескольких кандидатов и звание, затем «Обоснование и публикация».")[:2000]

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Кандидаты (можно несколько)", min_values=1,
                       max_values=10, row=0)
    async def people(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.candidates = {u.id for u in select.values if not u.bot}
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(placeholder="В какое звание", options=[discord.SelectOption(label="—")], row=1)
    async def rank(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.rank_id = int(select.values[0])
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Обоснование и публикация", emoji="📝", style=discord.ButtonStyle.success, row=2)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.candidates or not self.rank_id:
            await interaction.response.send_message("Сначала выберите кандидатов и звание.", ephemeral=True)
            return
        await interaction.response.send_modal(PromoReasonModal(self))


async def start_promotion(interaction: discord.Interaction) -> None:
    if not is_officer(interaction.user):
        await deny(interaction)
        return
    view = PromoSetupView(interaction.guild)
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


# ---------- панель ----------

class SenatePanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Личное дело", emoji="📂", style=discord.ButtonStyle.primary, custom_id="sen:dossier")
    async def dossier(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        await interaction.response.send_message("Выберите легионера:", view=PickMemberView(), ephemeral=True)

    @discord.ui.button(label="Чёрный список", emoji="⛔", style=discord.ButtonStyle.danger, custom_id="sen:blacklist")
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        await interaction.response.send_message(embed=blacklist_embed(), view=BlacklistView(interaction.user),
                                                ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Представление к повышению", emoji="🎖️", style=discord.ButtonStyle.success,
                       custom_id="sen:promo", row=1)
    async def promotion(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await start_promotion(interaction)

    @discord.ui.button(label="Открытые представления", emoji="📋", style=discord.ButtonStyle.secondary,
                       custom_id="sen:promos", row=1)
    async def promotions(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        items = db.open_promos()
        if not items:
            await interaction.response.send_message("Открытых представлений нет.", ephemeral=True)
            return
        lines = []
        for p in items[:20]:
            yes, no = db.promo_votes(p["id"])
            cands = " ".join(f"<@{c['user_id']}>" for c in db.promo_candidates(p["id"]))
            lines.append(f"**№{p['id']}** → <@&{p['rank_id']}>: {cands}\n👍 {len(yes)} · 👎 {len(no)} · "
                         f"[открыть]({promo_link(p)})")
        await interaction.response.send_message(
            embed=discord.Embed(title=f"🎖️ Открытые представления: {len(items)}",
                                description="\n\n".join(lines)[:4000], color=discord.Color.dark_gold()),
            ephemeral=True)

    @discord.ui.button(label="Новое поручение", emoji="📌", style=discord.ButtonStyle.success,
                       custom_id="sen:task", row=3)
    async def new_task(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.tasks import start_task
        await start_task(interaction)

    @discord.ui.button(label="Поручения", emoji="🗂️", style=discord.ButtonStyle.secondary, custom_id="sen:tasks",
                       row=3)
    async def task_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        from cogs import tasks
        await interaction.response.send_message(embed=tasks.list_embed(tasks.db.open_tasks(), "🗂️ Открытые поручения"),
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Кадры", emoji="👥", style=discord.ButtonStyle.primary, custom_id="sen:staff", row=2)
    async def staff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        from cogs.reports import personnel_embeds
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embeds=personnel_embeds(interaction.guild), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Доклад за неделю", emoji="📊", style=discord.ButtonStyle.secondary,
                       custom_id="sen:report", row=2)
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        from cogs.reports import weekly_embed
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=weekly_embed(interaction.guild), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Выговоры", emoji="⚠️", style=discord.ButtonStyle.secondary, custom_id="sen:warnings")
    async def warnings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        rows = db.conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(ts) AS last FROM notes WHERE kind = 'warning' AND active = 1 "
            "GROUP BY user_id ORDER BY n DESC, last DESC").fetchall()
        if not rows:
            await interaction.response.send_message("Действующих выговоров нет.", ephemeral=True)
            return
        lines = [f"<@{r['user_id']}> — **{r['n']}**{' ⚠️' if r['n'] >= WARN_ALERT else ''} · последний <t:{r['last']}:d>"
                 for r in rows[:40]]
        await interaction.response.send_message(
            embed=discord.Embed(title="⚠️ Действующие выговоры", description="\n".join(lines)[:4000],
                                color=discord.Color.orange()), ephemeral=True)


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🏛️ СЕНАТ",
        description="Дела, чёрный список, выговоры, повышения, поручения и отчёты — в меню ниже.\n"
                    "Каждый видит только доступные ему действия.",
        color=discord.Color.dark_gold(),
    )


class Senate(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != config.GUILD_ID:
            return
        matches = blacklist_matches(member.id)
        if not matches:
            return
        channel = await senate_channel(self.bot)
        if channel:
            await channel.send(
                (f"⛔ **ВНИМАНИЕ:** на сервер зашёл человек из чёрного списка — {member.mention} (`{member.id}`)\n"
                 + "\n".join(bl_line(b) for b in matches[:5]))[:2000],
                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="дело", description="Личное дело легионера (только офицеры)")
    @app_commands.describe(участник="Чьё дело открыть")
    @app_commands.guild_only()
    async def dossier(self, interaction: discord.Interaction, участник: discord.Member) -> None:
        await show_dossier(interaction, участник)

    @app_commands.command(name="представление", description="Представить к повышению (офицеры)")
    @app_commands.guild_only()
    async def promotion_cmd(self, interaction: discord.Interaction) -> None:
        await start_promotion(interaction)

    @app_commands.command(name="сенат", description="Опубликовать и закрепить панель Сената в этом канале")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from cogs.compact import senate_panel
            message = await interaction.channel.send(embed=panel_embed(), view=senate_panel()())
        except discord.HTTPException as e:
            await interaction.followup.send(f"Не удалось опубликовать панель: {e.text or e}", ephemeral=True)
            return
        try:
            await message.pin(reason="Панель Сената")
            note = "и закреплена"
        except discord.HTTPException:
            note = "но закрепить не вышло: дайте боту право «Закреплять сообщения» или закрепите вручную"
        await interaction.followup.send(f"✅ Панель опубликована {note}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_view(SenatePanel())
    bot.add_dynamic_items(PromoButton)
    await bot.add_cog(Senate(bot))
    bot.tree.add_command(dossier_menu)
