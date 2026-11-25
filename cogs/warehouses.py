"""Секретариум: учёт складов Легиона.

Как это работает:
1. /склад добавить — форма (название, гекс, регион, назначение, комментарий),
   затем выбор владельца и категории, затем пароль.
2. Бот публикует карточку склада в «Секретариуме» (офицерские — в отдельный канал,
   если он задан). Пароль в карточке никогда не показывается.
3. Кнопки на карточке: пароль (в личку), изменить, место, владелец, смена пароля,
   статус/категория, журнал. Права проверяются перед каждым действием.
4. /склад найти — поиск по названию/номеру, гексу, региону, владельцу, назначению, статусу.
5. Все действия пишутся в журнал (база data/legion.db, таблица audit_log).
6. Склад в Foxhole удаляется, если его не обновлять 50 часов. Бот хранит время
   последнего обновления (кнопка «Обновил») и напоминает за 12 ч и за 1 ч:
   обычные склады — в «Секретариум», личные — владельцу в личку.
"""
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("warehouses")

DB_PATH: Path = config.DATA_DIR / "legion.db"
SCHEMA_VERSION = 2

EXPIRE_HOURS = 50  # через столько часов без обновления склад удаляется игрой
# (часов с последнего обновления, номер напоминания)
REMINDERS = [(38, 1), (49, 2), (50, 3)]

STATUSES = {
    "active": ("🟢 Активен", discord.Color.green()),
    "maintenance": ("🟡 На обслуживании", discord.Color.gold()),
    "moved": ("🔵 Перемещён", discord.Color.blue()),
    "closed": ("⚫ Закрыт", discord.Color.dark_grey()),
}
CATEGORIES = {"general": "📦 Общий", "officer": "🎖️ Офицерский", "personal": "👤 Личный"}

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
    "refresh": "отметил обновление склада",
    "war_reset": "закрыл склад при сбросе складов",
    "delete": "удалил склад",
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
            c.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '1')")
            c.commit()
            log.info("База складов создана: %s", DB_PATH)
        if version < 2:
            # Миграция 1 → 2: время создания в игре, последнее обновление, этап напоминаний.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(warehouses)")}
            if "game_created_at" not in cols:
                c.execute("ALTER TABLE warehouses ADD COLUMN game_created_at INTEGER")
            if "refreshed_at" not in cols:
                c.execute("ALTER TABLE warehouses ADD COLUMN refreshed_at INTEGER")
            if "reminder_stage" not in cols:
                c.execute("ALTER TABLE warehouses ADD COLUMN reminder_stage INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE warehouses SET game_created_at = created_at WHERE game_created_at IS NULL")
            c.execute("UPDATE warehouses SET refreshed_at = created_at WHERE refreshed_at IS NULL")
            c.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            c.commit()
            log.info("База складов обновлена до версии %d", SCHEMA_VERSION)
        # Сообщения-напоминания об обновлении (чтобы удалять их после обновления склада)
        c.execute("CREATE TABLE IF NOT EXISTS reminder_msgs (warehouse_id INTEGER NOT NULL, "
                  "channel_id INTEGER NOT NULL, message_id INTEGER PRIMARY KEY)")
        c.commit()
        if not c.execute("SELECT 1 FROM permissions LIMIT 1").fetchone():
            self.reset_permissions()

    # склады
    def create(self, data: dict, created_by: int) -> int:
        now = int(time.time())
        game_created = data.get("game_created_at") or now
        cur = self.conn.execute(
            "INSERT INTO warehouses (name, owner_id, region, coords, password, purpose, comment, category,"
            " status, created_by, created_at, updated_at, game_created_at, refreshed_at, reminder_stage)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, 0)",
            (
                data["name"], data["owner_id"], data["region"], data["coords"], data["password"],
                data["purpose"], data["comment"], data["category"], created_by, now, now,
                game_created, game_created,
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

    # напоминания
    def add_reminder_msg(self, wid: int, channel_id: int, message_id: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO reminder_msgs VALUES (?, ?, ?)", (wid, channel_id, message_id))
        self.conn.commit()

    def pop_reminder_msgs(self, wid: int) -> list[tuple[int, int]]:
        rows = [(r["channel_id"], r["message_id"]) for r in self.conn.execute(
            "SELECT channel_id, message_id FROM reminder_msgs WHERE warehouse_id = ?", (wid,))]
        self.conn.execute("DELETE FROM reminder_msgs WHERE warehouse_id = ?", (wid,))
        self.conn.commit()
        return rows

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


def is_admin(member: discord.abc.User) -> bool:
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator or member.guild.owner_id == member.id
    )


def can(member: discord.abc.User, wh: dict, action: str) -> bool:
    """Можно ли этому человеку сделать action с этим складом."""
    if not can_see(member, wh):
        return False
    if action in ("password", "edit", "set_password", "audit", "refresh") and is_owner(member, wh):
        return True
    # Пароль личного склада — только владельцу (и администраторам)
    if wh["category"] == "personal" and action in ("password", "set_password", "refresh"):
        return is_admin(member)
    if action == "refresh":
        return has(member, "password")
    return has(member, action)


def allowed_categories(member: discord.abc.User, wh: Optional[dict] = None) -> list[str]:
    """Какие категории человек может поставить складу."""
    personal_ok = wh is None or is_owner(member, wh) or is_admin(member)
    if wh is not None and wh["category"] == "personal" and not personal_ok:
        return []  # личный склад может перевести только владелец
    result = ["general"]
    if has(member, "officer"):
        result.append("officer")
    if personal_ok:
        result.append("personal")
    return result


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

def expires_at(wh: dict) -> int:
    return int(wh["refreshed_at"]) + EXPIRE_HOURS * 3600


STATUS_DOTS = {"active": "🟢", "maintenance": "🟡", "moved": "🔵", "closed": "⚫"}


def card_embed(wh: dict) -> discord.Embed:
    """Компактная карточка: 3–4 строки вместо таблицы полей."""
    status, color = STATUSES.get(wh["status"], STATUSES["active"])
    title = f"📦 №{wh['id']} · {wh['name']}"
    if wh["category"] != "general":
        title += " " + CATEGORIES[wh["category"]].split()[0]
    lines = []
    place = wh["region"] + (f" / {wh['coords']}" if wh["coords"] else "")
    lines.append(f"📍 {place} · 👤 <@{wh['owner_id']}>")
    meta = []
    if wh["purpose"]:
        meta.append(f"🎯 {wh['purpose']}")
    if wh["status"] != "active":
        meta.append(status)
    if meta:
        lines.append(" · ".join(meta))
    if wh["status"] != "closed":
        expires = expires_at(wh)
        tail = (f"⚠️ срок истёк <t:{expires}:R>" if expires <= time.time()
                else f"⏳ удалится <t:{expires}:R>")
        lines.append(f"♻️ обновлён <t:{wh['refreshed_at']}:R> · {tail}")
    if wh["comment"]:
        lines.append(f"💬 {wh['comment'][:200]}")
    if wh["category"] == "personal":
        lines.append("🔒 пароль — только владельцу")
    e = discord.Embed(title=f"{STATUS_DOTS.get(wh['status'], '')} {title}", description="\n".join(lines),
                      color=color)
    return e


# Действия в меню «⋯ Ещё»
MENU_OPEN = [
    ("edit", "Изменить данные", "✏️"),
    ("move", "Изменить место", "📍"),
    ("owner", "Сменить владельца", "👤"),
    ("setpw", "Сменить пароль", "🔄"),
    ("status", "Статус / категория", "📊"),
    ("log", "Журнал", "📜"),
    ("delete", "Удалить склад", "🗑️"),
]
MENU_CLOSED = [
    ("status", "Статус / категория", "📊"),
    ("log", "Журнал", "📜"),
    ("delete", "Удалить склад", "🗑️"),
]
MENU_VERSION = "2"  # меняется, когда в меню карточки добавляются пункты — старые карточки обновятся при запуске


class CardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wh:(?P<action>[a-z]+):(?P<wid>[0-9]+)"):
    """Кнопка карточки склада. Работает и после перезапуска бота (в том числе на старых карточках)."""

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


class CardMenu(discord.ui.DynamicItem[discord.ui.Select], template=r"whm:(?P<wid>[0-9]+)"):
    """Меню «⋯ Ещё» на карточке склада."""

    def __init__(self, wid: int, closed: bool = False) -> None:
        options = [discord.SelectOption(label=label, value=action, emoji=emoji)
                   for action, label, emoji in (MENU_CLOSED if closed else MENU_OPEN)]
        super().__init__(discord.ui.Select(placeholder="⋯ Ещё", options=options, custom_id=f"whm:{wid}"))
        self.wid = wid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match, /):
        menu = cls(int(match["wid"]))
        menu.item.options = item.options
        return menu

    async def callback(self, interaction: discord.Interaction) -> None:
        action = interaction.data.get("values", [""])[0]
        await on_card_button(interaction, action, self.wid)


def card_view(wh: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if wh["status"] != "closed":
        view.add_item(CardButton("pw", wh["id"], "Пароль", "🔑", discord.ButtonStyle.success, 0))
        view.add_item(CardButton("refresh", wh["id"], "Обновил", "♻️", discord.ButtonStyle.primary, 0))
    view.add_item(CardMenu(wh["id"], closed=wh["status"] == "closed"))
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
    region = discord.ui.TextInput(label="Гекс", placeholder="Например: Deadlands", max_length=60)
    coords = discord.ui.TextInput(
        label="Регион", placeholder="Например: Abandoned Ward", max_length=60, required=False
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
        view = CreateView(draft, allowed_categories(interaction.user))
        await interaction.response.send_message(view.summary(), view=view, ephemeral=True)


class CreateView(discord.ui.View):
    def __init__(self, draft: dict, categories: list[str]) -> None:
        super().__init__(timeout=900)
        self.draft = draft
        self.done = False
        self.category.options = [
            discord.SelectOption(label=CATEGORIES[key], value=key) for key in categories
        ]

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

    @discord.ui.select(placeholder="Категория: общий склад", options=[discord.SelectOption(label="—")], row=1)
    async def category(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["category"] = select.values[0]
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Задать пароль и сохранить", emoji="🔑", style=discord.ButtonStyle.success, row=2)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PasswordModal(create_view=self))


def parse_hours_ago(text: str) -> Optional[float]:
    """«5», «1,5», «3ч» → часы. Пусто → 0. Ошибка → None."""
    text = text.strip().lower().replace(",", ".").rstrip("чh").strip()
    if not text:
        return 0.0
    try:
        hours = float(text)
    except ValueError:
        return None
    return hours if 0 <= hours <= 24 * 30 else None


class PasswordModal(discord.ui.Modal):
    password = discord.ui.TextInput(label="Пароль склада", max_length=64)
    created_ago = discord.ui.TextInput(
        label="Создан в игре примерно … часов назад",
        placeholder="Пусто — только что. Например: 5 или 1,5",
        max_length=10,
        required=False,
    )

    def __init__(self, *, create_view: Optional[CreateView] = None, wid: Optional[int] = None) -> None:
        super().__init__(title="Новый склад · шаг 3 из 3" if create_view else "Смена пароля склада")
        self.create_view = create_view
        self.wid = wid
        if create_view is None:
            self.remove_item(self.created_ago)

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
        hours = parse_hours_ago(self.created_ago.value)
        if hours is None:
            await interaction.response.send_message(
                "Не понял, сколько часов назад создан склад. Напишите число, например 5 или 1,5, "
                "и снова нажмите «Задать пароль и сохранить».",
                ephemeral=True,
            )
            return
        draft = dict(
            view.draft,
            password=self.password.value.strip(),
            game_created_at=int(time.time() - hours * 3600),
        )
        if draft["category"] not in allowed_categories(user):
            draft["category"] = "general"

        view.done = True
        view.stop()
        await interaction.response.edit_message(content="⏳ Сохраняю склад…", view=None)

        wid = db.create(draft, created_by=user.id)
        wh = db.get(wid)
        await audit(
            interaction.client, user, wh, "create",
            f"{CATEGORIES[wh['category']]}, {wh['region']}" + (f" / {wh['coords']}" if wh["coords"] else "")
            + f", создан в игре <t:{wh['game_created_at']}:f>",
        )
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
    region = discord.ui.TextInput(label="Гекс", placeholder="Например: Deadlands", max_length=60)
    coords = discord.ui.TextInput(label="Регион", placeholder="Например: Abandoned Ward", max_length=60, required=False)

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
        diff = changes(wh, new, {"region": "Гекс", "coords": "Регион"})
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

        categories = [c for c in allowed_categories(member, wh) if c != wh["category"]]
        if can_edit and categories and wh["status"] != "closed":
            self.category_select.options = [
                discord.SelectOption(label=CATEGORIES[key], value=key) for key in categories
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
            await clear_reminders(interaction.client, wh["id"])
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
        new = select.values[0]
        if wh is None or not can(interaction.user, wh, "edit") or new not in allowed_categories(interaction.user, wh):
            await deny(interaction)
            return
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
            f"Гекс: {wh['region']}" + (f", регион: {wh['coords']}" if wh["coords"] else "") + "\n"
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

    elif action == "refresh":
        if wh["status"] == "closed" or not can(member, wh, "refresh"):
            await deny(interaction)
            return
        now = int(time.time())
        db.update(wh["id"], refreshed_at=now, reminder_stage=0)
        await interaction.response.send_message(
            f"♻️ Отмечено: склад №{wh['id']} обновлён <t:{now}:f>. "
            f"Следующий срок — <t:{now + EXPIRE_HOURS * 3600}:f>.",
            ephemeral=True,
        )
        await audit(interaction.client, member, wh, "refresh")
        await clear_reminders(interaction.client, wh["id"])
        error = await publish_card(interaction.client, wh["id"])
        if error:
            await interaction.followup.send(error, ephemeral=True)

    elif action == "log":
        if not can(member, wh, "audit"):
            await deny(interaction)
            return
        await interaction.response.send_message(embed=history_embed(wh), ephemeral=True)

    elif action == "delete":
        if not can_delete(member, wh):
            await deny(interaction, "⛔ Удалить склад могут его владелец, Консулы и Принцепс.")
            return
        await interaction.response.send_modal(DeleteModal(wh["id"], wh["name"]))


def can_delete(member: discord.abc.User, wh: dict) -> bool:
    """Владелец склада, Консулы, Принцепс (и владелец сервера)."""
    return (is_owner(member, wh) or can_reset(member)
            or (bool(config.CONSUL_ROLE_ID) and any(r.id == config.CONSUL_ROLE_ID
                                                    for r in getattr(member, "roles", []))))


class DeleteModal(discord.ui.Modal):
    confirm = discord.ui.TextInput(label="Для подтверждения напишите: УДАЛИТЬ", max_length=10)

    def __init__(self, wid: int, name: str) -> None:
        super().__init__(title=f"🗑️ Удалить склад «{name}»"[:45])
        self.wid = wid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wh = db.get(self.wid)
        if wh is None:
            await deny(interaction, "Склад уже удалён.")
            return
        if not can_delete(interaction.user, wh):
            await deny(interaction)
            return
        if self.confirm.value.strip().upper() != "УДАЛИТЬ":
            await interaction.response.send_message("Удаление отменено: подтверждение не совпало.", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ Склад №{wh['id']} «{wh['name']}» удалён из списка.",
                                                ephemeral=True)
        await delete_warehouse(interaction.client, interaction.user, wh)


async def delete_warehouse(client: discord.Client, user: discord.abc.User, wh: dict) -> None:
    owner = f"владелец <@{wh['owner_id']}>"
    await audit(client, user, wh, "delete", f"{owner}; место: {wh['region']}" + (f" / {wh['coords']}" if wh['coords'] else ""))
    await clear_reminders(client, wh["id"])
    if wh["message_id"] and wh["channel_id"]:
        channel = client.get_channel(wh["channel_id"])
        if channel is not None:
            try:
                await channel.get_partial_message(wh["message_id"]).delete()
            except discord.HTTPException:
                pass
    db.conn.execute("DELETE FROM reminder_msgs WHERE warehouse_id = ?", (wh["id"],))
    db.conn.execute("DELETE FROM warehouses WHERE id = ?", (wh["id"],))
    db.conn.commit()


# ---------- напоминания об обновлении ----------

async def clear_reminders(client: discord.Client, wid: int) -> None:
    """Удаляет все напоминания об обновлении этого склада (в Секретариуме и в личке)."""
    for channel_id, message_id in db.pop_reminder_msgs(wid):
        try:
            await client.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass


async def send_reminder(client: discord.Client, wh: dict, stage: int) -> None:
    expires = expires_at(wh)
    place = wh["region"] + (f" / {wh['coords']}" if wh["coords"] else "")
    name = f"№{wh['id']} «{wh['name']}» ({place})"
    if jump_url(wh):
        name = f"[{name}]({jump_url(wh)})"
    if stage >= 3:
        text = f"❗ Склад {name} не обновлялся {EXPIRE_HOURS} ч — возможно, удалён игрой."
    else:
        text = f"⏰ Обновите склад {name} — удалится <t:{expires}:R>."
    view = discord.ui.View(timeout=None)
    view.add_item(CardButton("refresh", wh["id"], "Обновил", "♻️", discord.ButtonStyle.success))
    await clear_reminders(client, wh["id"])  # остаётся только самое свежее напоминание

    if wh["category"] == "personal":
        try:
            owner = client.get_user(wh["owner_id"]) or await client.fetch_user(wh["owner_id"])
            msg = await owner.send(text, view=view)
            db.add_reminder_msg(wh["id"], msg.channel.id, msg.id)
            log.info("Напоминание %d о складе %s отправлено владельцу в личку", stage, wh["id"])
            return
        except discord.HTTPException:
            log.info("Личка владельца склада %s закрыта — пишу в Секретариум", wh["id"])

    channel = client.get_channel(config.SECRETARIUM_CHANNEL_ID) or await client.fetch_channel(
        config.SECRETARIUM_CHANNEL_ID
    )
    msg = await channel.send(
        f"<@{wh['owner_id']}> {text}",
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    db.add_reminder_msg(wh["id"], msg.channel.id, msg.id)
    log.info("Напоминание %d о складе %s отправлено в Секретариум", stage, wh["id"])


# ---------- списки складов и закреплённая панель ----------

async def send_list(interaction: discord.Interaction, items: list[dict], title: str, empty: str) -> None:
    """Компактный список складов, который видит только нажавший."""
    if not items:
        await interaction.response.send_message(empty, ephemeral=True)
        return
    lines = []
    for w in items[:25]:
        place = w["region"] + (f" / {w['coords']}" if w["coords"] else "")
        name = f"**№{w['id']} {w['name']}**"
        if jump_url(w):
            name = f"[{name}]({jump_url(w)})"
        mark = STATUS_DOTS.get(w["status"], "")
        if w["category"] != "general":
            mark += CATEGORIES[w["category"]].split()[0]
        lines.append(f"{mark} {name} — {place} · <@{w['owner_id']}> · ♻️ <t:{w['refreshed_at']}:R>")
    embed = discord.Embed(title=f"{title}: {len(items)}", description="\n".join(lines)[:4000],
                          color=discord.Color.dark_red())
    if len(items) > 25:
        embed.set_footer(text="Показаны первые 25 — точнее: /склад найти")
    await interaction.response.send_message(embed=embed, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


def sort_key(w: dict) -> tuple:
    return (w["region"].casefold(), (w["coords"] or "").casefold(), w["name"].casefold())


class PanelView(discord.ui.View):
    """Закреплённая панель «Секретариума»: кнопка «Добавить» и меню. Работает после перезапуска."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Добавить склад", emoji="➕", style=discord.ButtonStyle.success, custom_id="whp:add")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not has(interaction.user, "create"):
            await deny(interaction)
            return
        await interaction.response.send_modal(WarehouseForm())

    @discord.ui.select(placeholder="📋 Склады…", custom_id="whp:menu", options=[
        discord.SelectOption(label="Все склады", value="list", emoji="📋"),
        discord.SelectOption(label="Мои склады", value="mine", emoji="👤"),
        discord.SelectOption(label="Сброс складов (Принцепс)", value="reset", emoji="⚠️"),
    ])
    async def menu(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        choice = select.values[0]
        if choice == "list":
            await self.list_all(interaction)
        elif choice == "mine":
            await self.mine(interaction)
        elif choice == "reset":
            if not can_reset(interaction.user):
                await deny(interaction, "⛔ Сброс складов доступен только Принцепсу.")
                return
            await interaction.response.send_modal(WarResetModal())

    async def list_all(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "search"):
            await deny(interaction)
            return
        items = sorted(
            (w for w in db.all() if w["status"] != "closed" and can_see(interaction.user, w)), key=sort_key
        )
        await send_list(interaction, items, "📋 Склады Легиона", "Складов пока нет.")

    async def mine(self, interaction: discord.Interaction) -> None:
        items = sorted(
            (w for w in db.all() if w["status"] != "closed" and w["owner_id"] == interaction.user.id), key=sort_key
        )
        await send_list(interaction, items, "👤 Ваши склады", "У вас пока нет складов.")

    # Старые кнопки на ранее опубликованных панелях продолжают работать
    @discord.ui.button(label="Все склады", custom_id="whp:list", row=4)
    async def old_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.list_all(interaction)

    @discord.ui.button(label="Мои склады", custom_id="whp:mine", row=4)
    async def old_mine(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.mine(interaction)

    @discord.ui.button(label="Сброс складов", custom_id="whp:reset", row=4)
    async def old_reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not can_reset(interaction.user):
            await deny(interaction, "⛔ Сброс складов доступен только Принцепсу.")
            return
        await interaction.response.send_modal(WarResetModal())


def panel_view() -> discord.ui.View:
    """Панель для публикации: без «старых» кнопок."""
    view = PanelView()
    for item in list(view.children):
        if getattr(item, "custom_id", "") in ("whp:list", "whp:mine", "whp:reset"):
            view.remove_item(item)
    return view


# ---------- сброс войны ----------

def can_reset(member: discord.abc.User) -> bool:
    """Сброс войны — только владелец сервера или Принцепс."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild.owner_id == member.id:
        return True
    return bool(config.PRINCEPS_ROLE_ID) and any(r.id == config.PRINCEPS_ROLE_ID for r in member.roles)


class WarResetModal(discord.ui.Modal, title="⚠️ Сброс складов"):
    war = discord.ui.TextInput(label="Номер новой войны (необязательно)", max_length=10, required=False,
                               placeholder="Например: 128")
    confirm = discord.ui.TextInput(label="Для подтверждения напишите: СБРОС", max_length=10)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_reset(interaction.user):
            await deny(interaction)
            return
        if self.confirm.value.strip().upper() != "СБРОС":
            await interaction.response.send_message("Сброс отменён: подтверждение не совпало.", ephemeral=True)
            return
        open_count = sum(1 for w in db.all() if w["status"] != "closed")
        view = WarResetChoice(self.war.value.strip().lstrip("№#"))
        await interaction.response.send_message(
            f"⚠️ Будут закрыты все открытые склады: **{open_count}**. Перед этим бот сделает резервную копию.\n"
            "Что сделать с карточками в канале?",
            view=view, ephemeral=True,
        )


class WarResetChoice(discord.ui.View):
    def __init__(self, war: str) -> None:
        super().__init__(timeout=300)
        self.war = war
        self.done = False

    async def _run(self, interaction: discord.Interaction, delete_cards: bool) -> None:
        if self.done or not can_reset(interaction.user):
            await deny(interaction)
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Сбрасываю склады… Это может занять минуту.", view=None)
        client = interaction.client
        user = interaction.user

        # 1. Резервная копия до изменений
        backup_note = ""
        try:
            from cogs.backup import send_backup
            if config.AUDIT_CHANNEL_ID:
                await send_backup(client, f"перед сбросом складов ({user})")
                backup_note = "резервная копия отправлена в «Инвентариум»"
        except Exception as e:  # копия не должна мешать сбросу, но сообщим
            log.error("Не удалось сделать копию перед сбросом: %s", e)
            backup_note = "⚠️ резервную копию сделать не удалось"

        # 2. Закрываем склады
        label = f"война №{self.war}" if self.war else "новая война"
        closed = 0
        failed = 0
        for wh in db.all():
            if wh["status"] == "closed":
                continue
            db.update(wh["id"], status="closed")
            db.log(user.id, wh["id"], "war_reset", label)
            await clear_reminders(client, wh["id"])
            closed += 1
            if delete_cards and wh["message_id"] and wh["channel_id"]:
                channel = client.get_channel(wh["channel_id"])
                try:
                    if channel is not None:
                        await channel.get_partial_message(wh["message_id"]).delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    failed += 1
                db.update(wh["id"], touch=False, message_id=None)
            elif not delete_cards:
                if await publish_card(client, wh["id"]):
                    failed += 1

        if self.war:
            db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('war', ?)", (self.war,))
            db.conn.commit()

        # 3. Журнал
        summary = (f"⚠️ <t:{int(time.time())}:f> · {user.mention} выполнил **сброс складов** ({label}): "
                   f"закрыто складов — {closed}, карточки {'удалены' if delete_cards else 'оставлены'}.")
        if config.AUDIT_CHANNEL_ID:
            channel = client.get_channel(config.AUDIT_CHANNEL_ID)
            if channel is not None:
                try:
                    await channel.send(summary, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
        log.info("Сброс войны (%s): закрыто %d складов (%s)", label, closed, user)

        text = f"✅ Сброс выполнен ({label}): закрыто складов — **{closed}**."
        if backup_note:
            text += f"\n💾 {backup_note}."
        if failed:
            text += f"\n⚠️ Не удалось обработать карточек: {failed} (проверьте права бота в канале)."
        text += "\nДанные складов сохранены в базе и журнале."
        await interaction.edit_original_response(content=text)

    @discord.ui.button(label="Закрыть и удалить карточки", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, delete_cards=True)

    @discord.ui.button(label="Закрыть, карточки оставить", emoji="📦", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, delete_cards=False)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Сброс отменён.", view=None)


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🏛️ СЕКРЕТАРИУМ",
        description="Учёт складов Легиона. Пароли — только в личку, кнопкой 🔑 на карточке.\n"
                    "Обновили склад в игре — жмите ♻️.",
        color=discord.Color.dark_red(),
    )


# ---------- команды ----------

def _autocomplete(interaction: discord.Interaction, current: str, column: str) -> list[app_commands.Choice[str]]:
    cur = current.casefold()
    values = sorted({w[column] for w in db.all() if w[column] and can_see(interaction.user, w)}, key=str.casefold)
    return [app_commands.Choice(name=v, value=v) for v in values if cur in v.casefold()][:25]


async def hex_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return _autocomplete(interaction, current, "region")


async def subregion_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return _autocomplete(interaction, current, "coords")


ACTION_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in ACTIONS.items()]
STATUS_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in STATUSES.items()]


class Warehouses(commands.Cog):
    sklad = app_commands.Group(name="склад", description="Учёт складов Легиона", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminders.start()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Один раз после обновления меню: старые карточки получают новые пункты «⋯ Ещё»."""
        row = db.conn.execute("SELECT value FROM meta WHERE key = 'menu_version'").fetchone()
        if row and row[0] == MENU_VERSION:
            return
        db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('menu_version', ?)", (MENU_VERSION,))
        db.conn.commit()
        for wh in db.all():
            if not (wh["message_id"] and wh["channel_id"]):
                continue
            channel = self.bot.get_channel(wh["channel_id"])
            if channel is None:
                continue
            try:
                await channel.get_partial_message(wh["message_id"]).edit(view=card_view(wh))
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)

    async def cog_unload(self) -> None:
        self.reminders.cancel()

    @tasks.loop(minutes=5)
    async def reminders(self) -> None:
        now = time.time()
        for wh in db.all():
            if wh["status"] == "closed" or not wh["refreshed_at"]:
                continue
            hours = (now - wh["refreshed_at"]) / 3600
            stage = max((n for h, n in REMINDERS if hours >= h), default=0)
            if stage > wh["reminder_stage"]:
                db.update(wh["id"], touch=False, reminder_stage=stage)
                try:
                    await send_reminder(self.bot, wh, stage)
                except Exception:
                    log.exception("Не удалось отправить напоминание о складе %s", wh["id"])
                if stage == 3:
                    await publish_card(self.bot, wh["id"])  # пометить «срок истёк»

    @reminders.before_loop
    async def before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    @sklad.command(name="добавить", description="Зарегистрировать новый склад")
    async def add(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "create"):
            await deny(interaction)
            return
        await interaction.response.send_modal(WarehouseForm())

    @sklad.command(name="найти", description="Поиск складов")
    @app_commands.describe(
        запрос="Название или номер склада",
        гекс="Гекс, например Deadlands",
        регион="Регион внутри гекса, например Abandoned Ward",
        владелец="Владелец склада",
        назначение="Назначение склада",
        статус="Статус (по умолчанию — все, кроме закрытых)",
    )
    @app_commands.choices(статус=STATUS_CHOICES)
    @app_commands.autocomplete(гекс=hex_autocomplete, регион=subregion_autocomplete)
    async def find(
        self,
        interaction: discord.Interaction,
        запрос: Optional[str] = None,
        гекс: Optional[str] = None,
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
            if гекс and гекс.casefold() not in w["region"].casefold():
                return False
            if регион and регион.casefold() not in w["coords"].casefold():
                return False
            if владелец and w["owner_id"] != владелец.id:
                return False
            if назначение and назначение.casefold() not in w["purpose"].casefold():
                return False
            return True

        found = [w for w in db.all() if matches(w)]
        await send_list(interaction, found, "🔎 Найдено складов", "Ничего не найдено.")

    @sklad.command(name="панель", description="Опубликовать и закрепить панель складов в Секретариуме")
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "perms"):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = self.bot.get_channel(config.SECRETARIUM_CHANNEL_ID) or await self.bot.fetch_channel(
                config.SECRETARIUM_CHANNEL_ID
            )
            message = await channel.send(embed=panel_embed(), view=panel_view())
        except discord.HTTPException as e:
            await interaction.followup.send(f"Не удалось опубликовать панель: {e.text or e}", ephemeral=True)
            return
        try:
            await message.pin(reason="Панель складов")
            note = "и закреплена"
        except discord.HTTPException:
            note = (
                "но закрепить её не вышло: дайте боту право «Закреплять сообщения» "
                "(или «Управлять сообщениями») либо закрепите вручную"
            )
        await interaction.followup.send(f"✅ Панель опубликована {note}: {message.jump_url}", ephemeral=True)

    @sklad.command(name="обновить_карточки", description="Перерисовать все карточки складов в текущем виде")
    async def redraw(self, interaction: discord.Interaction) -> None:
        if not has(interaction.user, "perms"):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        done = failed = 0
        for w in db.all():
            if not w["message_id"]:
                continue
            if await publish_card(self.bot, w["id"]):
                failed += 1
            else:
                done += 1
        text = f"✅ Карточек обновлено: {done}."
        if failed:
            text += f" Не удалось: {failed}."
        await interaction.followup.send(text, ephemeral=True)

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
            "менять и получать его пароль и смотреть его журнал. Пароль личного склада получает "
            "только владелец.*",
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
    bot.add_dynamic_items(CardButton, CardMenu)
    bot.add_view(PanelView())
    await bot.add_cog(Warehouses(bot))
