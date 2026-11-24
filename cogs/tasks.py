"""Поручения Сената.

Выдают: Принцепс, Консулы, Сенаторы (и администраторы).
Исполнители — один или несколько человек. Поручение публикуется карточкой в канале,
где его создали; исполнители получают уведомление в личку.

- «✅ Выполнено» (с отчётом) — исполнитель или тот, кто выдал.
- «🚫 Отменить» — тот, кто выдал, или Принцепс/Консулы.
- Напоминание исполнителям за сутки до срока и уведомление о просрочке.
Данные — в data/senate.db (таблицы tasks, task_assignees).
"""
import logging
import sqlite3
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.consulate import has_any, is_admin

log = logging.getLogger("tasks")

STATUS = {
    "open": ("🟡 В работе", discord.Color.gold()),
    "done": ("✅ Выполнено", discord.Color.green()),
    "cancelled": ("🚫 Отменено", discord.Color.dark_grey()),
}


# ---------- база (в файле Сената) ----------

class DB:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER,
                title       TEXT    NOT NULL,
                details     TEXT    NOT NULL DEFAULT '',
                author_id   INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                deadline    INTEGER,
                status      TEXT    NOT NULL DEFAULT 'open',
                report      TEXT    NOT NULL DEFAULT '',
                closed_by   INTEGER,
                closed_at   INTEGER,
                reminded    INTEGER NOT NULL DEFAULT 0      -- 1: за сутки, 2: просрочено
            );
            CREATE TABLE IF NOT EXISTS task_assignees (
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (task_id, user_id)
            );
            """
        )
        self.conn.commit()

    def create(self, channel_id: int, title: str, details: str, author_id: int, deadline: Optional[int],
               assignees: list[int]) -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks (channel_id, title, details, author_id, created_at, deadline) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, title, details, author_id, int(time.time()), deadline))
        tid = cur.lastrowid
        for uid in assignees:
            self.conn.execute("INSERT OR IGNORE INTO task_assignees VALUES (?, ?)", (tid, uid))
        self.conn.commit()
        return tid

    def get(self, tid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def set(self, tid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), tid))
        self.conn.commit()

    def assignees(self, tid: int) -> list[int]:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM task_assignees WHERE task_id = ?", (tid,))]

    def open_tasks(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND message_id IS NOT NULL "
            "ORDER BY deadline IS NULL, deadline, id")]

    def open_for(self, user_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT t.* FROM tasks t JOIN task_assignees a ON a.task_id = t.id "
            "WHERE a.user_id = ? AND t.status = 'open' AND t.message_id IS NOT NULL "
            "ORDER BY t.deadline IS NULL, t.deadline", (user_id,))]

    def done_count(self, user_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM tasks t JOIN task_assignees a ON a.task_id = t.id "
            "WHERE a.user_id = ? AND t.status = 'done'", (user_id,)).fetchone()[0]


db: Optional[DB] = None


# ---------- права ----------

def can_assign(member: discord.abc.User) -> bool:
    """Принцепс, Консулы, Сенаторы (и администраторы)."""
    roles = {r for r in (config.PRINCEPS_ROLE_ID, config.CONSUL_ROLE_ID, config.SENATE_ROLE_ID) if r}
    return is_admin(member) or has_any(member, roles)


def can_manage(member: discord.abc.User, t: dict) -> bool:
    """Отменить: кто выдал, Принцепс, Консулы, администраторы."""
    roles = {r for r in (config.PRINCEPS_ROLE_ID, config.CONSUL_ROLE_ID) if r}
    return member.id == t["author_id"] or is_admin(member) or has_any(member, roles)


# ---------- оформление ----------

def task_link(t: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{t['channel_id']}/{t['message_id']}"


def overdue(t: dict) -> bool:
    return t["status"] == "open" and t["deadline"] is not None and t["deadline"] < time.time()


def task_embed(t: dict) -> discord.Embed:
    label, color = STATUS[t["status"]]
    if overdue(t):
        label, color = "🔴 Просрочено", discord.Color.red()
    e = discord.Embed(title=f"📌 ПОРУЧЕНИЕ №{t['id']}: {t['title']}", description=t["details"] or None, color=color)
    e.add_field(name="Исполнители", value=" ".join(f"<@{u}>" for u in db.assignees(t["id"]))[:1024] or "—",
                inline=False)
    e.add_field(name="Выдал", value=f"<@{t['author_id']}>", inline=True)
    if t["deadline"]:
        e.add_field(name="Срок", value=f"<t:{t['deadline']}:f>\n<t:{t['deadline']}:R>", inline=True)
    else:
        e.add_field(name="Срок", value="без срока", inline=True)
    e.add_field(name="Статус", value=label, inline=True)
    if t["status"] != "open" and t["closed_by"]:
        line = f"<@{t['closed_by']}> · <t:{t['closed_at']}:f>"
        if t["report"]:
            line += f"\n{t['report']}"
        e.add_field(name="Отчёт" if t["status"] == "done" else "Отменил", value=line[:1024], inline=False)
    return e


class TaskButton(discord.ui.DynamicItem[discord.ui.Button], template=r"task:(?P<tid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, tid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"task:{tid}:{act}"))
        self.tid = tid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["tid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        t = db.get(self.tid)
        if t is None or t["status"] != "open":
            await interaction.response.send_message("Поручение уже закрыто.", ephemeral=True)
            return
        if self.act == "done":
            if interaction.user.id not in db.assignees(t["id"]) and not can_manage(interaction.user, t) \
                    and interaction.user.id != t["author_id"]:
                await interaction.response.send_message("⛔ Отметить выполнение может исполнитель или тот, кто выдал.",
                                                        ephemeral=True)
                return
            await interaction.response.send_modal(ReportModal(t["id"], interaction.message))
        elif self.act == "cancel":
            if not can_manage(interaction.user, t):
                await interaction.response.send_message("⛔ Отменить может тот, кто выдал, или Принцепс и Консулы.",
                                                        ephemeral=True)
                return
            db.set(t["id"], status="cancelled", closed_by=interaction.user.id, closed_at=int(time.time()))
            await interaction.response.edit_message(embed=task_embed(db.get(t["id"])), view=None)
            await notify(interaction.client, db.get(t["id"]),
                         f"🚫 Поручение №{t['id']} «{t['title']}» отменено.")


def task_view(t: dict) -> Optional[discord.ui.View]:
    if t["status"] != "open":
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(TaskButton(t["id"], "done", "Выполнено", "✅", discord.ButtonStyle.success))
    view.add_item(TaskButton(t["id"], "cancel", "Отменить", "🚫", discord.ButtonStyle.secondary))
    return view


async def notify(client: discord.Client, t: dict, text: str) -> None:
    """Личное сообщение всем исполнителям."""
    for uid in db.assignees(t["id"]):
        user = client.get_user(uid)
        if user is None:
            try:
                user = await client.fetch_user(uid)
            except discord.HTTPException:
                continue
        try:
            await user.send(f"{text}\n{task_link(t)}")
        except discord.HTTPException:
            pass


class ReportModal(discord.ui.Modal, title="Поручение выполнено"):
    report = discord.ui.TextInput(label="Краткий отчёт (необязательно)", style=discord.TextStyle.paragraph,
                                  max_length=500, required=False)

    def __init__(self, tid: int, message: discord.Message) -> None:
        super().__init__()
        self.tid = tid
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = db.get(self.tid)
        if t is None or t["status"] != "open":
            await interaction.response.send_message("Поручение уже закрыто.", ephemeral=True)
            return
        db.set(self.tid, status="done", report=self.report.value.strip(), closed_by=interaction.user.id,
               closed_at=int(time.time()))
        t = db.get(self.tid)
        await interaction.response.edit_message(embed=task_embed(t), view=None)
        if interaction.user.id != t["author_id"]:
            author = interaction.client.get_user(t["author_id"])
            if author:
                try:
                    await author.send(f"✅ Поручение №{t['id']} «{t['title']}» выполнено "
                                      f"({interaction.user.display_name}). {task_link(t)}")
                except discord.HTTPException:
                    pass


# ---------- создание ----------

def parse_deadline(text: str) -> Optional[int]:
    """«23.11 21:00», «21:00», «через 2д» или просто «30.11» (до конца дня)."""
    from cogs.operations import parse_start
    from cogs.profiles import parse_until
    return parse_start(text) or parse_until(text)


class TaskForm(discord.ui.Modal, title="Новое поручение"):
    task_title = discord.ui.TextInput(label="Что сделать (коротко)", max_length=100,
                                      placeholder="Например: наладить снабжение Deadlands")
    details = discord.ui.TextInput(label="Подробности (необязательно)", style=discord.TextStyle.paragraph,
                                   max_length=1500, required=False)

    def __init__(self, assignees: list[int], setup_view: "TaskSetupView") -> None:
        super().__init__()
        self.assignees = assignees
        self.setup_view = setup_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_assign(interaction.user):
            await interaction.response.send_message("⛔ Поручения выдают Принцепс, Консулы и Сенаторы.", ephemeral=True)
            return
        from cogs.timepick import when_ok, when_ts
        err = when_ok(self.setup_view)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        deadline = when_ts(self.setup_view)
        if self.setup_view.done:
            return
        self.setup_view.done = True
        self.setup_view.stop()
        await interaction.response.edit_message(content="⏳ Публикую…", view=None)
        tid = db.create(interaction.channel.id, self.task_title.value.strip(), self.details.value.strip(),
                        interaction.user.id, deadline, self.assignees)
        t = db.get(tid)
        try:
            message = await interaction.channel.send(
                content=" ".join(f"<@{u}>" for u in self.assignees), embed=task_embed(t), view=task_view(t),
                allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e:
            db.set(tid, status="cancelled")
            await interaction.edit_original_response(content=f"Не удалось опубликовать: {e.text or e}")
            return
        db.set(tid, message_id=message.id)
        t = db.get(tid)
        when = f" Срок: <t:{deadline}:f>." if deadline else ""
        await notify(interaction.client, t, f"📌 Вам поручение №{tid}: **{t['title']}**.{when}")
        await interaction.edit_original_response(content=f"✅ Поручение №{tid} выдано: {message.jump_url}")
        log.info("Поручение %s выдано (%s)", tid, interaction.user)


class TaskSetupView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)
        from cogs.timepick import add_when
        self.assignees: list[int] = []
        self.done = False
        add_when(self, "deadline", 1, 2)

    def summary(self) -> str:
        from cogs.timepick import when_text
        names = " ".join(f"<@{u}>" for u in self.assignees) or "не выбраны"
        return (f"📌 **Новое поручение**\nИсполнители: {names}\nСрок: {when_text(self)}\n"
                "Дальше — «Текст».")

    async def on_when(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Кому поручить (можно несколько)", max_values=10, row=0)
    async def people(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.assignees = [u.id for u in select.values if not u.bot]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Текст", emoji="📝", style=discord.ButtonStyle.success, row=3)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.assignees:
            await interaction.response.send_message("Сначала выберите исполнителей.", ephemeral=True)
            return
        await interaction.response.send_modal(TaskForm(self.assignees, self))


async def start_task(interaction: discord.Interaction) -> None:
    if not can_assign(interaction.user):
        await interaction.response.send_message("⛔ Поручения выдают Принцепс, Консулы и Сенаторы.", ephemeral=True)
        return
    view = TaskSetupView()
    await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


def list_embed(items: list[dict], title: str) -> discord.Embed:
    lines = []
    for t in items[:20]:
        who = " ".join(f"<@{u}>" for u in db.assignees(t["id"]))
        when = f"до <t:{t['deadline']}:R>" if t["deadline"] else "без срока"
        mark = "🔴 " if overdue(t) else ""
        lines.append(f"{mark}**№{t['id']} · {t['title']}** — {who} · {when} · [открыть]({task_link(t)})")
    text = "\n".join(lines) or "Открытых поручений нет."
    if len(items) > 20:
        text += f"\n…и ещё {len(items) - 20}"
    return discord.Embed(title=f"{title}: {len(items)}", description=text[:4000], color=discord.Color.gold())


# ---------- ког ----------

class Tasks(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.ticker.start()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @tasks.loop(minutes=5)
    async def ticker(self) -> None:
        now = time.time()
        for t in db.open_tasks():
            if not t["deadline"]:
                continue
            try:
                if t["reminded"] < 1 and t["deadline"] - 86400 <= now < t["deadline"] \
                        and t["deadline"] - t["created_at"] > 86400:
                    db.set(t["id"], reminded=1)
                    await notify(self.bot, t, f"⏰ Через сутки срок поручения №{t['id']}: **{t['title']}** "
                                              f"(<t:{t['deadline']}:f>).")
                if t["reminded"] < 2 and now >= t["deadline"]:
                    db.set(t["id"], reminded=2)
                    await notify(self.bot, t, f"🔴 Поручение №{t['id']} **{t['title']}** просрочено.")
                    channel = self.bot.get_channel(t["channel_id"])
                    if channel is not None:
                        try:
                            await channel.get_partial_message(t["message_id"]).edit(embed=task_embed(db.get(t["id"])))
                        except discord.HTTPException:
                            pass
            except Exception:
                log.exception("Ошибка при обработке поручения %s", t["id"])

    @ticker.before_loop
    async def before_ticker(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="поручение", description="Выдать поручение (Принцепс, Консулы, Сенаторы)")
    @app_commands.guild_only()
    async def new_task(self, interaction: discord.Interaction) -> None:
        await start_task(interaction)

    @app_commands.command(name="мои_поручения", description="Мои открытые поручения")
    @app_commands.guild_only()
    async def my_tasks(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=list_embed(db.open_for(interaction.user.id), "📌 Ваши поручения"),
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    global db
    from cogs import senate
    db = DB(senate.db.conn)
    bot.add_dynamic_items(TaskButton)
    await bot.add_cog(Tasks(bot))
