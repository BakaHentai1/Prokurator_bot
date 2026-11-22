"""Сенат: закрытые инструменты офицеров (Центурион и выше).

- Чёрный список: ник в Foxhole и/или Discord, причина. Бот предупреждает офицеров,
  если человек из списка подаёт анкету или заходит на сервер.
- Личное дело: заметки офицеров (видны только офицерам) и выговоры (человек получает
  уведомление в личку). Открывается кнопкой на панели, командой /дело или
  долгим нажатием на человека → Приложения → «Личное дело».
- /сенат — закреплённая панель.
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


db: Optional[DB] = None


# ---------- права ----------

def is_officer(member: discord.abc.User) -> bool:
    """Центурион и выше (и администраторы)."""
    return is_admin(member) or has_any(member, officer_roles())


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
        if not is_officer(interaction.user):
            await deny(interaction)
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
        if not (can_create(interaction.user) or interaction.user.id == b["author_id"]):
            await interaction.response.send_message("⛔ Убрать запись может её автор или Принцепс, Консулы, Сенат.",
                                                    ephemeral=True)
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
    def __init__(self) -> None:
        super().__init__(timeout=300)

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
                e.add_field(name="Статус", value=f"🏖️ в отпуске до <t:{vac['until']}:D>", inline=True)
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
        if not is_officer(interaction.user):
            await deny(interaction)
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
        await interaction.response.edit_message(embed=dossier_embed(self.member), view=DossierView(self.member))
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
        if not (can_create(interaction.user) or interaction.user.id == w["author_id"]):
            await interaction.response.send_message("⛔ Снять выговор может вынесший его или Принцепс, Консулы, Сенат.",
                                                    ephemeral=True)
            return
        db.deactivate(w["id"], interaction.user.id)
        await interaction.response.edit_message(content=f"✅ Выговор №{w['id']} снят.", view=None)
        try:
            await self.member.send(f"✅ Выговор снят: {w['text']}\n\n*Senatus Populusque Legionis*")
        except discord.HTTPException:
            pass


class DossierView(discord.ui.View):
    def __init__(self, member: discord.abc.User) -> None:
        super().__init__(timeout=900)
        self.member = member

    @discord.ui.button(label="Заметка", emoji="📝", style=discord.ButtonStyle.secondary)
    async def note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        await interaction.response.send_modal(NoteModal(self.member, "note"))

    @discord.ui.button(label="Выговор", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def warning(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        await interaction.response.send_modal(NoteModal(self.member, "warning"))

    @discord.ui.button(label="Снять выговор", emoji="✅", style=discord.ButtonStyle.secondary)
    async def unwarn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        warnings = db.active_warnings(self.member.id)
        if not warnings:
            await interaction.response.send_message("Действующих выговоров нет.", ephemeral=True)
            return
        await interaction.response.send_message("Выберите выговор:", view=RemoveWarningView(self.member, warnings),
                                                ephemeral=True)

    @discord.ui.button(label="В чёрный список", emoji="⛔", style=discord.ButtonStyle.secondary)
    async def blacklist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_officer(interaction.user):
            await deny(interaction)
            return
        await interaction.response.send_modal(BlacklistAddModal(self.member))


async def show_dossier(interaction: discord.Interaction, member: discord.abc.User) -> None:
    if not is_officer(interaction.user):
        await deny(interaction)
        return
    if member.bot:
        await interaction.response.send_message("У ботов нет личного дела.", ephemeral=True)
        return
    await interaction.response.send_message(embed=dossier_embed(member), view=DossierView(member), ephemeral=True)


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
        await interaction.response.send_message(embed=blacklist_embed(), view=BlacklistView(), ephemeral=True,
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
        title="🏛️ СЕНАТ ЛЕГИОНА",
        description=(
            "Закрытые инструменты офицеров.\n\n"
            "📂 **Личное дело** — звание, статус, выговоры, заметки офицеров о легионере. "
            "Также: долгое нажатие на человека → Приложения → «Личное дело».\n"
            "⛔ **Чёрный список** — шпионы и нежелательные игроки. Бот предупредит, если они подадут анкету "
            "или зайдут на сервер.\n"
            "⚠️ **Выговоры** — у кого сколько действующих выговоров.\n\n"
            "*Senatus Populusque Legionis*"
        ),
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

    @app_commands.command(name="сенат", description="Опубликовать и закрепить панель Сената в этом канале")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await interaction.channel.send(embed=panel_embed(), view=SenatePanel())
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
    await bot.add_cog(Senate(bot))
    bot.tree.add_command(dossier_menu)
