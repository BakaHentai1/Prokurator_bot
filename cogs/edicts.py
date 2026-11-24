"""Эдикты Консулата — официальные объявления.

/эдикт (или кнопка на панели Консулата) — форма: заголовок, текст, картинка, время
публикации (пусто — сразу). Затем: кого упомянуть и нужна ли кнопка «Принято».

- Эдикты нумеруются, подписываются автором и его высшим званием.
- Кнопка «Принято» считает ознакомившихся; офицеры видят, кто именно.
- /эдикт_изменить — правка опубликованного, /эдикт_отменить — снять запланированный.
- Журнал (публикация, правка, отмена) — в «Архивариум».
- Данные хранятся в отдельном файле data/edicts.db.
"""
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
from cogs.consulate import archive, can_create, has_any, is_admin, officer_roles
from cogs.operations import parse_start, tz_name

log = logging.getLogger("edicts")

DB_PATH: Path = config.DATA_DIR / "edicts.db"
PINGS = {
    "legion": "🔔 Упомянуть Легионеров",
    "everyone": "📣 Упомянуть всех (@everyone)",
    "officers": "🎖️ Упомянуть офицеров",
    "none": "🔕 Без упоминания",
}


# ---------- база ----------

class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS edicts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id   INTEGER NOT NULL,
                message_id   INTEGER,
                title        TEXT    NOT NULL,
                body         TEXT    NOT NULL,
                image        TEXT    NOT NULL DEFAULT '',
                author_id    INTEGER NOT NULL,
                author_sign  TEXT    NOT NULL,
                ping         TEXT    NOT NULL DEFAULT 'legion',
                ack          INTEGER NOT NULL DEFAULT 1,
                publish_at   INTEGER NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'scheduled',  -- scheduled / published / cancelled
                created_at   INTEGER NOT NULL,
                edited_at    INTEGER
            );
            CREATE TABLE IF NOT EXISTS acks (
                edict_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                ts       INTEGER NOT NULL,
                PRIMARY KEY (edict_id, user_id)
            );
            """
        )
        self.conn.commit()

    def create(self, e: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO edicts (channel_id, title, body, image, author_id, author_sign, ping, ack, publish_at,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e["channel_id"], e["title"], e["body"], e["image"], e["author_id"], e["author_sign"], e["ping"],
             int(e["ack"]), e["publish_at"], int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, eid: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM edicts WHERE id = ?", (eid,)).fetchone()
        return dict(row) if row else None

    def set(self, eid: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE edicts SET {cols} WHERE id = ?", (*fields.values(), eid))
        self.conn.commit()

    def due(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM edicts WHERE status = 'scheduled' AND publish_at <= ?", (int(time.time()),)
        )
        return [r["id"] for r in rows]

    def acknowledge(self, eid: int, user_id: int) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO acks VALUES (?, ?, ?)", (eid, user_id, int(time.time())))
        self.conn.commit()
        return cur.rowcount > 0

    def ack_count(self, eid: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM acks WHERE edict_id = ?", (eid,)).fetchone()[0]

    def ack_users(self, eid: int) -> list[int]:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM acks WHERE edict_id = ? ORDER BY ts", (eid,))]


db: Optional[DB] = None


# ---------- оформление ----------

def signature(member: discord.abc.User) -> str:
    """«Имя, Звание» — высшая роль человека на сервере."""
    name = member.display_name
    if isinstance(member, discord.Member) and member.top_role and not member.top_role.is_default():
        return f"{name}, {member.top_role.name}"
    return name


def edict_embed(e: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📜 ЭДИКТ №{e['id']} · {e['title']}",
        description=e["body"],
        color=discord.Color.dark_red(),
    )
    if e["image"]:
        embed.set_image(url=e["image"])
    footer = f"— {e['author_sign']} · Senatus Populusque Legionis"
    if e["edited_at"]:
        footer += " · изменён"
    embed.set_footer(text=footer)
    embed.timestamp = datetime.fromtimestamp(max(e["publish_at"], 0), timezone.utc)
    if e["ack"] and e["status"] == "published":
        embed.add_field(name="Ознакомились", value=str(db.ack_count(e["id"])), inline=False)
    return embed


def ping_content(ping: str) -> tuple[Optional[str], discord.AllowedMentions]:
    if ping == "everyone":
        return "@everyone", discord.AllowedMentions(everyone=True)
    if ping == "officers":
        roles = sorted(officer_roles())
        if roles:
            return " ".join(f"<@&{r}>" for r in roles), discord.AllowedMentions(roles=True)
    if ping == "legion" and config.MEMBER_ROLE_ID:
        return f"<@&{config.MEMBER_ROLE_ID}>", discord.AllowedMentions(roles=True)
    return None, discord.AllowedMentions.none()


class EdictButton(discord.ui.DynamicItem[discord.ui.Button], template=r"edict:(?P<eid>[0-9]+):(?P<act>[a-z]+)"):
    def __init__(self, eid: int, act: str, label: str = "…", emoji: Optional[str] = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"edict:{eid}:{act}"))
        self.eid = eid
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["eid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.act == "ack":
            await acknowledge(interaction, self.eid)
        elif self.act == "who":
            await show_acks(interaction, self.eid)


def edict_view(e: dict) -> Optional[discord.ui.View]:
    if not e["ack"]:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(EdictButton(e["id"], "ack", "Принято", "✅", discord.ButtonStyle.success))
    view.add_item(EdictButton(e["id"], "who", None, "👁️"))
    return view


def edict_link(e: dict) -> str:
    return f"https://discord.com/channels/{config.GUILD_ID}/{e['channel_id']}/{e['message_id']}"


# ---------- действия ----------

async def acknowledge(interaction: discord.Interaction, eid: int) -> None:
    e = db.get(eid)
    if e is None or e["status"] != "published":
        await interaction.response.send_message("Эдикт не найден.", ephemeral=True)
        return
    if db.acknowledge(eid, interaction.user.id):
        await interaction.response.edit_message(embed=edict_embed(e))
        await interaction.followup.send("✅ Отмечено: вы ознакомились с эдиктом.", ephemeral=True)
    else:
        await interaction.response.send_message("Вы уже отметили, что ознакомились.", ephemeral=True)


async def show_acks(interaction: discord.Interaction, eid: int) -> None:
    if not (is_admin(interaction.user) or has_any(interaction.user, officer_roles())):
        await interaction.response.send_message("⛔ Список видят только офицеры.", ephemeral=True)
        return
    users = db.ack_users(eid)
    text = " ".join(f"<@{u}>" for u in users) or "Пока никто."
    await interaction.response.send_message(
        f"👁️ Ознакомились с эдиктом №{eid} ({len(users)}):\n{text}"[:2000], ephemeral=True
    )


async def publish(client: discord.Client, eid: int) -> Optional[str]:
    """Публикует эдикт. Возвращает ссылку или None при ошибке."""
    e = db.get(eid)
    if e is None or e["status"] != "scheduled":
        return None
    db.set(eid, status="published", publish_at=min(e["publish_at"], int(time.time())))
    e = db.get(eid)
    content, mentions = ping_content(e["ping"])
    try:
        channel = client.get_channel(e["channel_id"]) or await client.fetch_channel(e["channel_id"])
        try:
            message = await channel.send(content=content, embed=edict_embed(e), view=edict_view(e),
                                         allowed_mentions=mentions)
        except discord.HTTPException:
            if not e["image"]:
                raise
            db.set(eid, image="")  # чаще всего мешает плохая ссылка на картинку
            e = db.get(eid)
            message = await channel.send(content=content, embed=edict_embed(e), view=edict_view(e),
                                         allowed_mentions=mentions)
    except discord.HTTPException as err:
        db.set(eid, status="scheduled")
        log.error("Не удалось опубликовать эдикт %s: %s", eid, err)
        return None
    db.set(eid, message_id=message.id)
    e = db.get(eid)
    await archive(client, f"📜 <t:{int(time.time())}:f> · <@{e['author_id']}> издал эдикт №{eid} · {message.jump_url}",
                  edict_embed(e))
    log.info("Эдикт %s опубликован", eid)
    return message.jump_url


# ---------- создание ----------

class EdictForm(discord.ui.Modal, title="Новый эдикт"):
    def __init__(self, edit: Optional[dict] = None) -> None:
        super().__init__(title=f"Изменить эдикт №{edit['id']}" if edit else "Новый эдикт")
        self.edit = edit
        self.e_title = discord.ui.TextInput(label="Заголовок", max_length=150,
                                            default=edit["title"] if edit else None)
        self.body = discord.ui.TextInput(label="Текст эдикта", style=discord.TextStyle.paragraph, max_length=4000,
                                         default=edit["body"] if edit else None)
        self.image = discord.ui.TextInput(label="Ссылка на картинку (необязательно)", max_length=400,
                                          required=False, placeholder="https://…",
                                          default=edit["image"] if edit else None)
        self.when = discord.ui.TextInput(label=f"Опубликовать ({tz_name()}) — пусто: сразу", max_length=30,
                                         required=False, placeholder="21:00 · 23.11 21:00 · через 2ч")
        for item in (self.e_title, self.body, self.image):
            self.add_item(item)
        if not edit:
            self.add_item(self.when)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        image = self.image.value.strip()
        if image and not image.startswith(("http://", "https://")):
            image = ""
        if self.edit:
            await self._save_edit(interaction, image)
            return

        publish_at = int(time.time())
        if self.when.value.strip():
            publish_at = parse_start(self.when.value)
            if publish_at is None:
                await interaction.response.send_message(
                    f"Не понял время публикации: «{self.when.value}». Примеры ({tz_name()}): `21:00`, "
                    "`23.11 21:00`, `через 2ч`.",
                    ephemeral=True,
                )
                return
        draft = {
            "channel_id": interaction.channel.id,
            "title": self.e_title.value.strip(),
            "body": self.body.value.strip(),
            "image": image,
            "author_id": interaction.user.id,
            "author_sign": signature(interaction.user),
            "ping": "legion",
            "ack": True,
            "publish_at": publish_at,
        }
        view = EdictSetupView(draft)
        await interaction.response.send_message(view.summary(), embed=view.preview(), view=view, ephemeral=True)

    async def _save_edit(self, interaction: discord.Interaction, image: str) -> None:
        e = db.get(self.edit["id"])
        if e is None or (interaction.user.id != e["author_id"] and not is_admin(interaction.user)):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db.set(e["id"], title=self.e_title.value.strip(), body=self.body.value.strip(), image=image,
               edited_at=int(time.time()))
        e = db.get(e["id"])
        if e["status"] == "published" and e["message_id"]:
            try:
                channel = interaction.client.get_channel(e["channel_id"]) or await interaction.client.fetch_channel(
                    e["channel_id"])
                await channel.get_partial_message(e["message_id"]).edit(embed=edict_embed(e))
            except discord.HTTPException as err:
                await interaction.followup.send(f"Изменения сохранены, но пост обновить не вышло: {err.text or err}",
                                                ephemeral=True)
                return
            await archive(interaction.client,
                          f"✏️ <t:{int(time.time())}:f> · {interaction.user.mention} изменил эдикт №{e['id']} · "
                          f"{edict_link(e)}", edict_embed(e))
        await interaction.followup.send(f"✅ Эдикт №{e['id']} изменён.", ephemeral=True)


class EdictSetupView(discord.ui.View):
    def __init__(self, draft: dict) -> None:
        super().__init__(timeout=900)
        self.draft = draft
        self.done = False

    def summary(self) -> str:
        d = self.draft
        when = "сразу" if d["publish_at"] <= time.time() + 60 else f"<t:{d['publish_at']}:f> (<t:{d['publish_at']}:R>)"
        return (
            "**Предпросмотр эдикта** (номер присвоится при сохранении)\n"
            f"Публикация: {when} · {PINGS[d['ping']]} · "
            f"{'✅ с кнопкой «Принято»' if d['ack'] else 'без кнопки «Принято»'}\n"
            "Измените настройки при необходимости и нажмите «Издать»."
        )

    def preview(self) -> discord.Embed:
        d = dict(self.draft, id="…", status="preview", edited_at=None)
        return edict_embed(d)

    @discord.ui.select(placeholder="Упоминание: Легионеры",
                       options=[discord.SelectOption(label=label, value=key) for key, label in PINGS.items()], row=0)
    async def ping(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["ping"] = select.values[0]
        await interaction.response.edit_message(content=self.summary(), embed=self.preview(), view=self)

    @discord.ui.select(placeholder="Кнопка «Принято»: да", options=[
        discord.SelectOption(label="С кнопкой «Принято»", value="1", emoji="✅"),
        discord.SelectOption(label="Без кнопки", value="0", emoji="➖"),
    ], row=1)
    async def ack(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.draft["ack"] = select.values[0] == "1"
        await interaction.response.edit_message(content=self.summary(), embed=self.preview(), view=self)

    @discord.ui.button(label="Издать", emoji="📜", style=discord.ButtonStyle.success, row=2)
    async def issue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.done:
            return
        if not can_create(interaction.user):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="⏳ Сохраняю…", embed=None, view=None)
        eid = db.create(self.draft)
        if self.draft["publish_at"] > time.time() + 60:
            await interaction.edit_original_response(
                content=f"🕰️ Эдикт №{eid} будет издан <t:{self.draft['publish_at']}:f> (<t:{self.draft['publish_at']}:R>). "
                f"Отменить: `/эдикт_отменить номер:{eid}`"
            )
            await archive(interaction.client,
                          f"🕰️ <t:{int(time.time())}:f> · {interaction.user.mention} запланировал эдикт №{eid} "
                          f"«{self.draft['title']}» на <t:{self.draft['publish_at']}:f>")
            return
        link = await publish(interaction.client, eid)
        if link:
            await interaction.edit_original_response(content=f"✅ Эдикт №{eid} издан: {link}")
        else:
            db.set(eid, status="cancelled")
            await interaction.edit_original_response(
                content="Не удалось опубликовать: проверьте права бота в канале (писать, встраивать ссылки, упоминать)."
            )


class Edicts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        for eid in db.due():
            try:
                await publish(self.bot, eid)
            except Exception:
                log.exception("Ошибка публикации эдикта %s", eid)

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="эдикт", description="Издать эдикт в этом канале")
    @app_commands.guild_only()
    async def new_edict(self, interaction: discord.Interaction) -> None:
        if not can_create(interaction.user):
            await interaction.response.send_message("⛔ Эдикты издают Принцепс, Консулы и Сенат.", ephemeral=True)
            return
        await interaction.response.send_modal(EdictForm())

    @app_commands.command(name="эдикт_изменить", description="Изменить текст эдикта")
    @app_commands.describe(номер="Номер эдикта")
    @app_commands.guild_only()
    async def edit_edict(self, interaction: discord.Interaction, номер: int) -> None:
        e = db.get(номер)
        if e is None or e["status"] == "cancelled":
            await interaction.response.send_message("Эдикт не найден.", ephemeral=True)
            return
        if interaction.user.id != e["author_id"] and not is_admin(interaction.user):
            await interaction.response.send_message("⛔ Изменить эдикт может автор или администратор.", ephemeral=True)
            return
        await interaction.response.send_modal(EdictForm(edit=e))

    @app_commands.command(name="эдикт_отменить", description="Отменить запланированный эдикт")
    @app_commands.describe(номер="Номер эдикта")
    @app_commands.guild_only()
    async def cancel_edict(self, interaction: discord.Interaction, номер: int) -> None:
        e = db.get(номер)
        if e is None or e["status"] != "scheduled":
            await interaction.response.send_message(
                "Такого запланированного эдикта нет. Опубликованный можно изменить или удалить вручную.",
                ephemeral=True,
            )
            return
        if interaction.user.id != e["author_id"] and not is_admin(interaction.user):
            await interaction.response.send_message("⛔ Отменить эдикт может автор или администратор.", ephemeral=True)
            return
        db.set(номер, status="cancelled")
        await interaction.response.send_message(f"🚫 Эдикт №{номер} отменён.", ephemeral=True)
        await archive(interaction.client,
                      f"🚫 <t:{int(time.time())}:f> · {interaction.user.mention} отменил запланированный эдикт "
                      f"№{номер} «{e['title']}»")


async def setup(bot: commands.Bot) -> None:
    global db
    db = DB(DB_PATH)
    bot.add_dynamic_items(EdictButton)
    await bot.add_cog(Edicts(bot))
