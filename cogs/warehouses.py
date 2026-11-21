"""Секретариум: учёт складов Легиона.

Как это работает:
1. /склад добавить — форма (название, регион, координаты, назначение, комментарий),
   затем выбор владельца и категории, затем пароль.
2. Бот публикует карточку склада в «Секретариуме» (офицерские — в отдельный канал,
   если он задан). Пароль в карточке никогда не показывается.
3. Кнопки на карточке: пароль (в личку), изменить, место, владелец, смена пароля,
   статус/категория, журнал. Права проверяются перед каждым действием.
4. /склад найти — поиск по названию/номеру, региону, владельцу, назначению, статусу.
5. Все действия пишутся в журнал (база data/legion.db, таблица audit_log).
"""
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("warehouses")

DB_PATH: Path = config.DATA_DIR / "legion.db"
SCHEMA_VERSION = 1

STATUSES = {
    "active": ("🟢 Активен", discord.Color.green()),
    "maintenance": ("🟡 На обслуживании", discord.Color.gold()),
    "moved": ("🔵 Перемещён", discord.Color.blue()),
    "closed": ("⚫ Закрыт", discord.Color.dark_grey()),
}
CATEGORIES = {"general": "📦 Общий", "officer": "🎖️ Офицерский"}

# Права, которые можно выдавать ролям
ACTIONS = {
    "search": "Поиск складов",
    "create": "Добавление складов",
    "password": "Получение паролей",
    "officer": "Офицерские склады (видеть, создавать, пароли)",
    "edit": "Изменение любых складов",
    "set_password": "Смена паролей любых складов",
    "archive": "Закрытие и архивация",
    "audit": "Журнал аудита",
    "perms": "Настройка прав",
}

AUDIT_LABELS = {
    "create": "создал склад",
    "edit": "изменил данные",
    "move": "изменил местоположение",
    "owner": "сменил владельца",
    "password_view": "получил пароль",
    "password_change": "сменил пароль",
    "status": "сменил статус",
    "category": "сменил категорию",
    "archive": "архивировал склад",
    "reopen": "вернул склад из архива",
}


def default_permissions() -> dict[str, list[int]]:
    """Права по умолчанию для должностей Легиона."""
    all_but_perms = [a for a in ACTIONS if a != "perms"]
    ranks = [
        (config.PRINCEPS_ROLE_ID, list(ACTIONS)),
        (config.SENATE_ROLE_ID, all_but_perms),
        (config.CONSUL_ROLE_ID, all_but_perms),
        (config.LEGATE_ROLE_ID, ["search", "create", "password", "officer", "edit", "set_password", "audit"]),
        (config.CENTURION_ROLE_ID, ["search", "password", "officer"]),
        (config.FABRUM_ROLE_ID, ["search", "create", "password"]),
        (config.MEMBER_ROLE_ID, ["search"]),
    ]
    result: dict[str, list[int]] = {a: [] for a in ACTIONS}
    for role_id, actions in ranks:
        if role_id:
            for action in actions:
                result[action].append(role_id)
    return result


# ---------- база данных ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version < 1:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS warehouses (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL,
                    owner_id    INTEGER NOT NULL,
                    region      TEXT    NOT NULL,
                    coords      TEXT    NOT NULL DEFAULT '',
                    password    TEXT    NOT NULL,
                    purpose     TEXT    NOT NULL DEFAULT '',
                    comment     TEXT    NOT NULL DEFAULT '',
                    category    TEXT    NOT NULL DEFAULT 'general',
                    status      TEXT    NOT NULL DEFAULT 'active',
                    channel_id  INTEGER,
                    message_id  INTEGER,
                    created_by  INTEGER NOT NULL,
                    created_at  INTEGER NOT NULL,
                    updated_at  INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           INTEGER NOT NULL,
                    user_id      INTEGER NOT NULL,
                    warehouse_id INTEGER,
                    action       TEXT    NOT NULL,
                    details      TEXT    NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_wh ON audit_log (warehouse_id);
                CREATE TABLE IF NOT EXISTS permissions (
                    action  TEXT    NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (action, role_id)
                );
                """
            )
            c.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            c.commit()
            log.info("База складов создана: %s (версия схемы %d)", DB_PATH, SCHEMA_VERSION)
        if not c.execute("SELECT 1 FROM permissions LIMIT 1").fetchone():
            self.reset_permissions()

    # склады
    def create(self, data: dict, created_by: int) -> int:
        now = int(time.time())
        cur = self.conn.execute(
            "INSERT INTO warehouses (name, owner_id, region, coords, password, purpose, comment, category,"
            " status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                data["name"], data["owner_id"], data["region"], data["coords"], data["password"],
                data["purpose"], data["comment"], data["category"], created_by, now, now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, wid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM warehouses WHERE id = ?", (wid,)).fetchone()
        return dict(row) if row else None

    def all(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM warehouses ORDER BY id")]

    def update(self, wid: int, touch: bool = True, **fields) -> None:
        if touch:
            fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE warehouses SET {cols} WHERE id = ?", (*fields.values(), wid))
        self.conn.commit()

    # журнал
    def log(self, user_id: int, wid: Optional[int], action: str, details: str = "") -> int:
        ts = int(time.time())
        self.conn.execute(
            "INSERT INTO audit_log (ts, user_id, warehouse_id, action, details) VALUES (?, ?, ?, ?, ?)",
            (ts, user_id, wid, action, details[:500]),
        )
        self.conn.commit()
        return ts

    def history(self, wid: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE warehouse_id = ? ORDER BY id DESC LIMIT ?", (wid, limit)
        )
        return [dict(r) for r in rows]

    # права
    def permissions(self) -> dict[str, set[int]]:
        result: dict[str, set[int]] = {a: set() for a in ACTIONS}
        for r in self.conn.execute("SELECT action, role_id FROM permissions"):
            result.setdefault(r["action"], set()).add(r["role_id"])
        return result

    def grant(self, action: str, role_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO permissions VALUES (?, ?)", (action, role_id))
        self.conn.commit()

    def revoke(self, action: str, role_id: int) -> None:
        self.conn.execute("DELETE FROM permissions WHERE action = ? AND role_id = ?", (action, role_id))
        self.conn.commit()

    def reset_permissions(self) -> None:
        self.conn.execute("DELETE FROM permissions")
        for action, roles in default_permissions().items():
            for role_id in roles:
                self.conn.execute("INSERT OR IGNORE INTO permissions VALUES (?, ?)", (action, role_id))
        self.conn.commit()
        log.info("Права на склады выставлены по умолчанию")


db: Optional[DB] = None


# ---------- проверка прав ----------

def has(member: discord.abc.User, action: str) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator or member.guild.owner_id == member.id:
        return True
    allowed = db.permissions().get(action, set())
    return any(role.id in allowed for role in member.roles)


def is_owner(member: discord.abc.User, wh: dict) -> bool:
    return wh["owner_id"] == member.id


def can_see(member: discord.abc.User, wh: dict) -> bool:
    return wh["category"] != "officer" or is_owner(member, wh) or has(member, "officer")


def can(member: discord.abc.User, wh: dict, action: str) -> bool:
    """Можно ли этому человеку сделать action с этим складом."""
    if not can_see(member, wh):
        return False
    if action in ("password", "edit", "set_password", "audit") and is_owner(member, wh):
        return True
    return has(member, action)


async def deny(interaction: discord.Interaction, text: str = "⛔ Недостаточно прав для этого действия.") -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# ---------- журнал ----------

async def audit(client: discord.Client, user: discord.abc.User, wh: dict, action: str, details: str = "") -> None:
    ts = db.log(user.id, wh["id"], action, details)
    log.info("Склад %s: %s %s %s", wh["id"], user, action, details)
    if not config.AUDIT_CHANNEL_ID:
        return
    channel = client.get_channel(config.AUDIT_CHANNEL_ID)
    if channel is None:
        return
    text = f"📜 <t:{ts}:f> · {user.mention} — {AUDIT_LABELS.get(action, action)}: склад №{wh['id']} «{wh['name']}»"
    if details:
        text += f"\n↳ {details}"
    try:
        await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.error("Не удалось написать в канал журнала: %s", e)


def changes(old: dict, new: dict, labels: dict[str, str]) -> str:
    parts = []
    for key, label in labels.items():
        if (old.get(key) or "") != (new.get(key) or ""):
            parts.append(f"{label}: «{old.get(key) or '—'}» → «{new.get(key) or '—'}»")
    return "; ".join(parts)


# ---------- карточка склада ----------

def card_embed(wh: dict) -> discord.Embed:
    status, color = STATUSES.get(wh["status"], STATUSES["active"])
    e = discord.Embed(title=f"Склад №{wh['id']} · {wh['name']}", color=color)
    e.add_field(name="Регион", value=wh["region"], inline=True)
    e.add_field(name="Координаты", value=wh["coords"] or "—", inline=True)
    e.add_field(name="Владелец", value=f"<@{wh['owner_id']}>", inline=True)
    e.add_field(name="Назначение", value=wh["purpose"] or "—", inline=True)
    e.add_field(name="Категория", value=CATEGORIES.get(wh["category"], wh["category"]), inline=True)
    e.add_field(name="Статус", value=status, inline=True)
    if wh["comment"]:
        e.add_field(name="Комментарий", value=wh["comment"], inline=False)
    e.add_field(name="Пароль", value="🔒 Выдаётся в личные сообщения по кнопке «Пароль»", inline=False)
    e.set_footer(text="Обновлено")
    e.timestamp = datetime.fromtimestamp(wh["updated_at"], timezone.utc)
    return e


OPEN_BUTTONS = [
    ("pw", "Пароль", "🔑", discord.ButtonStyle.success, 0),
    ("edit", "Изменить", "✏️", discord.ButtonStyle.secondary, 0),
    ("move", "Место", "📍", discord.ButtonStyle.secondary, 0),
    ("owner", "Владелец", "👤", discord.ButtonStyle.secondary, 0),
    ("setpw", "Сменить пароль", "🔄", discord.ButtonStyle.secondary, 1),
    ("status", "Статус", "📊", discord.ButtonStyle.secondary, 1),
    ("log", "Журнал", "📜", discord.ButtonStyle.secondary, 1),
]
CLOSED_BUTTONS = [
    ("status", "Статус", "📊", discord.ButtonStyle.secondary, 0),
    ("log", "Журнал", "📜", discord.ButtonStyle.secondary, 0),
]


class CardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wh:(?P<action>[a-z]+):(?P<wid>[0-9]+)"):
    """Кнопка карточки склада. Работает и после перезапуска бота."""

    def __init__(
        self,
        action: str,
        wid: int,
        label: str = "…",
        emoji: Optional[str] = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        row: Optional[int] = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, row=row, custom_id=f"wh:{action}:{wid}")
        )
        self.action = action
        self.wid = wid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["wid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await on_card_button(interaction, self.action, self.wid)


def card_view(wh: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action, label, emoji, style, row in (CLOSED_BUTTONS if wh["status"] == "closed" else OPEN_BUTTONS):
        view.add_item(CardButton(action, wh["id"], label, emoji, style, row))
    return view


def target_channel_id(wh: dict) -> int:
    if wh["category"] == "officer" and config.OFFICER_WAREHOUSE_CHANNEL_ID:
        return config.OFFICER_WAREHOUSE_CHANNEL_ID
    return config.SECRETARIUM_CHANNEL_ID


def jump_url(wh: dict) -> Optional[str]:
    if wh.get("channel_id") and wh.get("message_id"):
        return f"https://discord.com/channels/{config.GUILD_ID}/{wh['channel_id']}/{wh['message_id']}"
    return None


async def publish_card(client: discord.Client, wid: int) -> Optional[str]:
    """Создаёт или обновляет карточку склада. Возвращает текст ошибки или None."""
    wh = db.get(wid)
    target_id = target_channel_id(wh)
    embed, view = card_embed(wh), card_view(wh)
    try:
        if wh["message_id"] and wh["channel_id"] == target_id:
            channel = client.get_channel(target_id)
            if channel is not None:
                try:
                    await channel.get_partial_message(wh["message_id"]).edit(embed=embed, view=view)
                    return None
                except discord.NotFound:
                    pass  # карточку удалили — опубликуем заново
        elif wh["message_id"] and wh["channel_id"]:
            old = client.get_channel(wh["channel_id"])
            if old is not None:
                try:
                    await old.get_partial_message(wh["message_id"]).delete()
                except discord.HTTPException:
                    pass

        channel = client.get_channel(target_id) or await client.fetch_channel(target_id)
        message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        db.update(wid, touch=False, channel_id=channel.id, message_id=message.id)
        return None
    except discord.HTTPException as e:
        log.error("Не удалось опубликовать карточку склада %s: %s", wid, e)
        return f"⚠️ Данные сохранены, но карточку обновить не удалось: проверьте права бота в канале ({e.text or e})."


def history_embed(wh: dict) -> discord.Embed:
    rows = db.history(wh["id"])
    lines = []
    for r in rows:
        line = f"<t:{r['ts']}:f> · <@{r['user_id']}> — {AUDIT_LABELS.get(r['action'], r['action'])}"
        if r["details"]:
            line += f"\n↳ {r['details']}"
        lines.append(line)
    text = "\n".join(lines) or "Записей пока нет."
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return discord.Embed(
        title=f"📜 Журнал склада №{wh['id']} · {wh['name']}",
        description=text,
        color=discord.Color.dark_gold(),
    ).set_footer(text="Последние 20 действий, новые сверху")


# ---------- создание склада ----------

class WarehouseForm(discord.ui.Modal, title="Новый склад · шаг 1 из 3"):
    name = discord.ui.TextInput(label="Название или номер склада", max_length=80)
    region = discord.ui.TextInput(label="Регион", placeholder="Например: Deadlands", max_length=60)
    coords = discord.ui.TextInput(
        label="Координаты на карте Foxhole", placeholder="Например: G7 k3", max_length=60, required=False
    )
    purpose = discord.ui.TextInput(
        label="Назначение", placeholder="Боеприпасы, техника, логистика, резерв…", max_length=100
    )
    comment = discord.ui.TextInput(
        label="Комментарий", style=discord.TextStyle.paragraph, max_length=500, required=False
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = {
            "name": self.name.value.strip(),
            "region": self.region.value.strip(),
            "coords": self.coords.value.strip(),
            "purpose": self.purpose.value.strip(),
            "comment": self.comment.value.strip(),
            "owner_id": interaction.user.id,
            "category": "general",
        }
        view = CreateView(draft, can_officer=has(interaction.user, "officer"))
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class CreateView(discord.ui.View):
    def __init__(self, draft: dict, can_officer: bool) -> None:
        super().__init__(timeout=900)
        self.draft = draft
        self.done = False
        if not can_officer:
            self.remove_item(self.category)

    def summary(self) -> str:
        return (
            f"**Шаг 2 из 3.** Склад «{self.draft['name']}» ({self.draft['region']})\n"
            f"Владелец: <@{self.draft['owner_id']}> · Категория: {CATEGORIES[self.draft['category']]}\n\n"
            "При необходимости выберите другого владельца и категорию, затем нажмите «Задать пароль»."
        )

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Владелец (по умолчанию — вы)", row=0)
    async def owner(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.draft["owner_id"] = select.values[0].id
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(
        placeholder="Категория: общий склад",
        options=[
            discord.SelectOption(label="Общий склад", value="general", emoji="📦"),
            discord.SelectOption(label="Офицерский склад", value="officer", emoji="🎖️"),
        ],
        row=1,
    )
    async def category(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["category"] = select.values[0]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Задать пароль и сохранить", emoji="🔑", style=discord.ButtonStyle.success, row=2)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PasswordModal(create_view=self))


class PasswordModal(discord.ui.Modal):
    password = discord.ui.TextInput(label="Пароль склада", max_length=64)

    def __init__(self, *, create_view: Optional[CreateView] = None, wid: Optional[int] = None) -> None:
        super().__init__(title="Новый склад · шаг 3 из 3" if create_view else "Смена пароля склада")
        self.create_view = create_view
        self.wid = wid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.create_view is not None:
            await self._create(interaction)
        else:
            await self._change(interaction)

    async def _create(self, interaction: discord.Interaction) -> None:
        view = self.create_view
        if view.done:
            await interaction.response.send_message("Этот склад уже сохранён.", ephemeral=True)
            return
        user = interaction.user
        if not has(user, "create"):
            await deny(interaction)
            return
        draft = dict(view.draft, password=self.password.value.strip())
        if draft["category"] == "officer" and not has(user, "officer"):
            draft["category"] = "general"

        view.done = True
        view.stop()
        await interaction.response.edit_message(content="⏳ Сохраняю склад…", view=None)

        wid = db.create(draft, created_by=user.id)
        wh = db.get(wid)
        await audit(interaction.client, user, wh, "create", f"{CATEGORIES[wh['category']]}, регион {wh['region']}")
        error = await publish_card(interaction.client, wid)
        wh = db.get(wid)
        text = f"✅ Склад №{wid} «{wh['name']}» зарегистрирован."
        if jump_url(wh):
            text += f"\nКарточка: {jump_url(wh)}"
        if error:
            text += f"\n{error}"
        await interaction.edit_original_response(content=text)

    async def _change(self, interaction: discord.Interaction) -> None:
        wh = db.get(self.wid)
        if wh is None:
            await deny(interaction, "Склад не найден.")
            return
        if not can(interaction.user, wh, "set_password"):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db.update(wh["id"], password=self.password.value.strip())
        await audit(interaction.client, interaction.user, wh, "password_change")
        error = await publish_card(interaction.client, wh["id"])
        await interaction.followup.send(error or "✅ Пароль изменён.", ephemeral=True)


# ---------- изменение склада ----------

class EditModal(discord.ui.Modal, title="Изменить склад"):
    name = discord.ui.TextInput(label="Название или номер склада", max_length=80)
    purpose = discord.ui.TextInput(label="Назначение", max_length=100)
    comment = discord.ui.TextInput(
        label="Комментарий", style=discord.TextStyle.paragraph, max_length=500, required=False
    )

    def __init__(self, wh: dict) -> None:
        super().__init__()
        self.wid = wh["id"]
        self.name.default = wh["name"]
        self.purpose.default = wh["purpose"]
        self.comment.default = wh["comment"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wh = db.get(self.wid)
        if wh is None or not can(interaction.user, wh, "edit"):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        new = {
            "name": self.name.value.strip(),
            "purpose": self.purpose.value.strip(),
            "comment": self.comment.value.strip(),
        }
        diff = changes(wh, new, {"name": "Название", "purpose": "Назначение", "comment": "Комментарий"})
        if not diff:
            await interaction.followup.send("Ничего не изменилось.", ephemeral=True)
            return
        db.update(wh["id"], **new)
        await audit(interaction.client, interaction.user, wh, "edit", diff)
        error = await publish_card(interaction.client, wh["id"])
        await interaction.followup.send(error or "✅ Данные склада обновлены.", ephemeral=True)


class MoveModal(discord.ui.Modal, title="Местоположение склада"):
    region = discord.ui.TextInput(label="Регион", max_length=60)
    coords = discord.ui.TextInput(label="Координаты на карте Foxhole", max_length=60, required=False)

    def __init__(self, wh: dict) -> None:
        super().__init__()
        self.wid = wh["id"]
        self.region.default = wh["region"]
        self.coords.default = wh["coords"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wh = db.get(self.wid)
        if wh is None or not can(interaction.user, wh, "edit"):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        new = {"region": self.region.value.strip(), "coords": self.coords.value.strip()}
        diff = changes(wh, new, {"region": "Регион", "coords": "Координаты"})
        if not diff:
            await interaction.followup.send("Ничего не изменилось.", ephemeral=True)
            return
        db.update(wh["id"], **new)
        await audit(interaction.client, interaction.user, wh, "move", diff)
        error = await publish_card(interaction.client, wh["id"])
        await interaction.followup.send(error or "✅ Местоположение обновлено.", ephemeral=True)


class OwnerView(discord.ui.View):
    def __init__(self, wid: int) -> None:
        super().__init__(timeout=300)
        self.wid = wid

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Новый владелец склада")
    async def owner(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        wh = db.get(self.wid)
        if wh is None or not can(interaction.user, wh, "edit"):
            await deny(interaction)
            return
        new_owner = select.values[0]
        if new_owner.id == wh["owner_id"]:
            await interaction.response.edit_message(content="Этот человек уже владелец склада.", view=None)
            return
        db.update(wh["id"], owner_id=new_owner.id)
        await interaction.response.edit_message(content=f"✅ Новый владелец: {new_owner.mention}", view=None)
        await audit(interaction.client, interaction.user, wh, "owner", f"<@{wh['owner_id']}> → {new_owner.mention}")
        error = await publish_card(interaction.client, wh["id"])
        if error:
            await interaction.followup.send(error, ephemeral=True)


class StatusView(discord.ui.View):
    def __init__(self, member: discord.Member, wh: dict) -> None:
        super().__init__(timeout=300)
        self.wid = wh["id"]
        can_archive = has(member, "archive")
        can_edit = can(member, wh, "edit")

        options = []
        for key, (label, _) in STATUSES.items():
            if key == wh["status"]:
                continue
            if key == "closed" or wh["status"] == "closed":
                if not can_archive:
                    continue
            elif not can_edit:
                continue
            options.append(discord.SelectOption(label=label, value=key))
        if options:
            self.status_select.options = options
        else:
            self.remove_item(self.status_select)

        if has(member, "officer") and wh["status"] != "closed":
            self.category_select.options = [
                discord.SelectOption(label=label, value=key)
                for key, label in CATEGORIES.items()
                if key != wh["category"]
            ]
        else:
            self.remove_item(self.category_select)

    @property
    def empty(self) -> bool:
        return not self.children

    @discord.ui.select(placeholder="Новый статус", options=[discord.SelectOption(label="—")])
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        wh = db.get(self.wid)
        new = select.values[0]
        if wh is None:
            await deny(interaction, "Склад не найден.")
            return
        archiving = new == "closed" or wh["status"] == "closed"
        allowed = can_see(interaction.user, wh) and (
            has(interaction.user, "archive") if archiving else can(interaction.user, wh, "edit")
        )
        if not allowed:
            await deny(interaction)
            return
        db.update(wh["id"], status=new)
        await interaction.response.edit_message(content=f"✅ Статус: {STATUSES[new][0]}", view=None)
        if new == "closed":
            action = "archive"
        elif wh["status"] == "closed":
            action = "reopen"
        else:
            action = "status"
        await audit(
            interaction.client, interaction.user, wh, action, f"{STATUSES[wh['status']][0]} → {STATUSES[new][0]}"
        )
        error = await publish_card(interaction.client, wh["id"])
        if error:
            await interaction.followup.send(error, ephemeral=True)

    @discord.ui.select(placeholder="Сменить категорию", options=[discord.SelectOption(label="—")])
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        wh = db.get(self.wid)
        if wh is None or not has(interaction.user, "officer"):
            await deny(interaction)
            return
        new = select.values[0]
        db.update(wh["id"], category=new)
        await interaction.response.edit_message(content=f"✅ Категория: {CATEGORIES[new]}", view=None)
        await audit(interaction.client, interaction.user, wh, "category", f"{CATEGORIES[wh['category']]} → {CATEGORIES[new]}")
        error = await publish_card(interaction.client, wh["id"])
        if error:
            await interaction.followup.send(error, ephemeral=True)


# ---------- кнопки карточки ----------

async def on_card_button(interaction: discord.Interaction, action: str, wid: int) -> None:
    member = interaction.user
    wh = db.get(wid)
    if wh is None:
        await deny(interaction, "Склад не найден в базе.")
        return
    if not can_see(member, wh):
        await deny(interaction)
        return

    if action == "pw":
        if wh["status"] == "closed" or not can(member, wh, "password"):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = (
            f"🔑 **Склад №{wh['id']} · {wh['name']}**\n"
            f"Регион: {wh['region']}" + (f", координаты: {wh['coords']}" if wh["coords"] else "") + "\n"
            f"Пароль: ||{wh['password']}||\n\n"
            "Не пересылайте пароль в общие каналы."
        )
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Не удалось написать вам в личку. Разрешите личные сообщения от участников сервера "
                "и нажмите «Пароль» ещё раз.",
                ephemeral=True,
            )
            return
        await audit(interaction.client, member, wh, "password_view")
        await interaction.followup.send("📨 Пароль отправлен вам в личные сообщения.", ephemeral=True)

    elif action == "edit":
        if not can(member, wh, "edit"):
            await deny(interaction)
            return
        await interaction.response.send_modal(EditModal(wh))

    elif action == "move":
        if not can(member, wh, "edit"):
            await deny(interaction)
            return
        await interaction.response.send_modal(MoveModal(wh))

    elif action == "owner":
        if not can(member, wh, "edit"):
            await deny(interaction)
            return
        await interaction.response.send_message(
            f"Выберите нового владельца склада №{wh['id']}:", view=OwnerView(wh["id"]), ephemeral=True
        )

    elif action == "setpw":
        if not can(member, wh, "set_password"):
            await deny(interaction)
            return
        await interaction.response.send_modal(PasswordModal(wid=wh["id"]))

    elif action == "status":
        view = StatusView(member, wh)
        if view.empty:
            await deny(interaction)
            return
        await interaction.response.send_message(
            f"Склад №{wh['id']} · сейчас: {STATUSES[wh['status']][0]}, {CATEGORIES[wh['category']]}",
            view=view,
            ephemeral=True,
        )

    elif action == "log":
        if not can(member, wh, "audit"):
            await deny(interaction)
            return
        await interaction.response.send_message(embed=history_embed(wh), ephemeral=True)


# ---------- команды ----------

async def region_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.casefold()
    regions = sorted({w["region"] for w in db.all() if can_see(interaction.user, w)}, key=str.casefold)
    return [app_commands.Choice(name=r, value=r) for r in regions if cur in r.casefold()][:25]


ACTION_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in ACTIONS.items()]
STATUS_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in STATUSES.items()]


class Warehouses(commands.Cog):
    sklad = app_commands.Group(name="склад", description="Учёт складов Легиона", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @sklad.command(name="добавить", description="Зарегистрировать новый склад")
    async def add(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "create"):
            await deny(interaction)
            return
        await interaction.response.send_modal(WarehouseForm())

    @sklad.command(name="найти", description="Поиск складов")
    @app_commands.describe(
        запрос="Название или номер склада",
        регион="Регион",
        владелец="Владелец склада",
        назначение="Назначение склада",
        статус="Статус (по умолчанию — все, кроме закрытых)",
    )
    @app_commands.choices(статус=STATUS_CHOICES)
    @app_commands.autocomplete(регион=region_autocomplete)
    async def find(
        self,
        interaction: discord.Interaction,
        запрос: Optional[str] = None,
        регион: Optional[str] = None,
        владелец: Optional[discord.Member] = None,
        назначение: Optional[str] = None,
        статус: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        user = interaction.user
        if not has(user, "search"):
            await deny(interaction)
            return

        def matches(w: dict) -> bool:
            if not can_see(user, w):
                return False
            if статус is not None:
                if w["status"] != статус.value:
                    return False
            elif w["status"] == "closed":
                return False
            if запрос:
                q = запрос.strip().lstrip("№#").casefold()
                if q != str(w["id"]) and q not in w["name"].casefold():
                    return False
            if регион and регион.casefold() not in w["region"].casefold():
                return False
            if владелец and w["owner_id"] != владелец.id:
                return False
            if назначение and назначение.casefold() not in w["purpose"].casefold():
                return False
            return True

        found = [w for w in db.all() if matches(w)]
        if not found:
            await interaction.response.send_message("Ничего не найдено.", ephemeral=True)
            return

        lines = []
        for w in found[:20]:
            line = f"**№{w['id']} · {w['name']}** — {w['region']}"
            if w["coords"]:
                line += f" ({w['coords']})"
            line += f"\n{STATUSES[w['status']][0]} · {w['purpose'] or '—'} · <@{w['owner_id']}>"
            if w["category"] == "officer":
                line += " · 🎖️"
            if jump_url(w):
                line += f" · [карточка]({jump_url(w)})"
            lines.append(line)
        embed = discord.Embed(
            title=f"🔎 Найдено складов: {len(found)}",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.dark_red(),
        )
        if len(found) > 20:
            embed.set_footer(text="Показаны первые 20 — уточните запрос")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sklad.command(name="журнал", description="Журнал действий со складом")
    @app_commands.describe(номер="Номер склада (№ на карточке)")
    async def history(self, interaction: discord.Interaction, номер: int) -> None:
        wh = db.get(номер)
        if wh is None or not can_see(interaction.user, wh):
            await deny(interaction, "Склад не найден.")
            return
        if not can(interaction.user, wh, "audit"):
            await deny(interaction)
            return
        await interaction.response.send_message(embed=history_embed(wh), ephemeral=True)

    @sklad.command(name="права", description="Показать, какие роли что могут делать со складами")
    async def show_perms(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "perms"):
            await deny(interaction)
            return
        perms = db.permissions()
        lines = []
        for key, label in ACTIONS.items():
            roles = " ".join(f"<@&{r}>" for r in sorted(perms.get(key, ()))) or "—"
            lines.append(f"**{label}**\n{roles}")
        embed = discord.Embed(
            title="🔐 Права на склады",
            description="\n\n".join(lines)
            + "\n\n*Администраторы сервера могут всё. Владелец склада может изменять свой склад, "
            "менять и получать его пароль и смотреть его журнал.*",
            color=discord.Color.dark_red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sklad.command(name="выдать_право", description="Разрешить роли действие со складами")
    @app_commands.describe(действие="Что разрешить", роль="Какой роли")
    @app_commands.choices(действие=ACTION_CHOICES)
    async def grant(
        self, interaction: discord.Interaction, действие: app_commands.Choice[str], роль: discord.Role
    ) -> None:
        if not has(interaction.user, "perms"):
            await deny(interaction)
            return
        db.grant(действие.value, роль.id)
        db.log(interaction.user.id, None, "perm_grant", f"{действие.name} → {роль.name}")
        await interaction.response.send_message(f"✅ {роль.mention}: разрешено «{действие.name}».", ephemeral=True)

    @sklad.command(name="снять_право", description="Запретить роли действие со складами")
    @app_commands.describe(действие="Что запретить", роль="Какой роли")
    @app_commands.choices(действие=ACTION_CHOICES)
    async def revoke(
        self, interaction: discord.Interaction, действие: app_commands.Choice[str], роль: discord.Role
    ) -> None:
        if not has(interaction.user, "perms"):
            await deny(interaction)
            return
        db.revoke(действие.value, роль.id)
        db.log(interaction.user.id, None, "perm_revoke", f"{действие.name} → {роль.name}")
        await interaction.response.send_message(f"✅ {роль.mention}: запрещено «{действие.name}».", ephemeral=True)

    @sklad.command(name="сбросить_права", description="Вернуть права на склады по умолчанию")
    async def reset(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "perms"):
            await deny(interaction)
            return
        db.reset_permissions()
        db.log(interaction.user.id, None, "perm_reset")
        await interaction.response.send_message("✅ Права сброшены по умолчанию.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    global db
    if not config.SECRETARIUM_CHANNEL_ID:
        log.warning("SECRETARIUM_CHANNEL_ID не задан — модуль складов выключен. Запустите: python setup.py")
        return
    db = DB(DB_PATH)
    bot.add_dynamic_items(CardButton)
    await bot.add_cog(Warehouses(bot))
