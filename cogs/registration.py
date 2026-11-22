"""Регистратум: анкета новичка и рассмотрение заявок офицерами.

Как это работает:
0. Когда человек заходит на сервер, бот выдаёт ему роль «Плебей», пишет в личку
   со ссылкой на «Регистратум», приветствует
   его в «Регистратуме» и прикладывает кнопки «Вступить в Легион», «Я союзник» и «Я варден».
   Приветствие удаляется, когда человек подал заявку или ушёл с сервера.
1. Администратор пишет /регистратум — бот публикует и закрепляет панель с кнопками.
2. Новичок жмёт кнопку и заполняет анкету.
3. Анкета уходит в закрытый канал анкет с кнопками «Принять» / «Отклонить».
4. Решение принимает человек с ролью рецензента. При принятии бот выдаёт роль
   легионера и пишет новичку в личку; при отказе — сообщает причину.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("registration")

DATA_FILE: Path = config.DATA_DIR / "applications.json"
GREETINGS_FILE: Path = config.DATA_DIR / "greetings.json"  # user_id → [channel_id, message_id]

STATUS_FIELD = "Статус"
STATUS_PENDING = "⏳ На рассмотрении"
COLOR_PENDING = discord.Color.gold()
COLOR_APPROVED = discord.Color.green()
COLOR_REJECTED = discord.Color.red()


# ---------- хранение заявок ----------

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("Файл %s повреждён, начинаю с пустого списка", DATA_FILE)
    return {}


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_greetings() -> dict:
    if GREETINGS_FILE.exists():
        try:
            return json.loads(GREETINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("Файл %s повреждён, начинаю с пустого списка", GREETINGS_FILE)
    return {}


def save_greetings(data: dict) -> None:
    GREETINGS_FILE.parent.mkdir(exist_ok=True)
    GREETINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remember_greeting(user_id: int, message: discord.Message) -> None:
    data = load_greetings()
    data[str(user_id)] = [message.channel.id, message.id]
    save_greetings(data)


async def delete_greeting(client: discord.Client, user_id: int) -> None:
    """Удаляет приветствие человека из Регистратума, если оно ещё висит."""
    data = load_greetings()
    entry = data.pop(str(user_id), None)
    if entry is None:
        return
    save_greetings(data)
    channel = client.get_channel(entry[0])
    if channel is None:
        return
    try:
        await channel.get_partial_message(entry[1]).delete()
        log.info("Удалил приветствие %s из Регистратума", user_id)
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        log.error("Не удалось удалить приветствие %s: %s", user_id, e)


def find_by_message(data: dict, message_id: int) -> tuple[str, dict] | tuple[None, None]:
    for user_id, app in data.items():
        if app.get("message_id") == message_id:
            return user_id, app
    return None, None


# ---------- проверки ----------

def is_reviewer(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id in config.REVIEWER_ROLE_IDS for role in member.roles)


def set_status(embed: discord.Embed, text: str, color: discord.Color) -> discord.Embed:
    embed.color = color
    for i, field in enumerate(embed.fields):
        if field.name == STATUS_FIELD:
            embed.set_field_at(i, name=STATUS_FIELD, value=text, inline=False)
            return embed
    embed.add_field(name=STATUS_FIELD, value=text, inline=False)
    return embed


async def notify(user: discord.abc.User, text: str) -> bool:
    """Пишет человеку в личку. Возвращает False, если личка закрыта."""
    try:
        await user.send(text)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def notify_with_view(user: discord.abc.User, text: str, view: discord.ui.View) -> bool:
    """Пишет человеку в личку сообщение с кнопками. False — если личка закрыта."""
    try:
        await user.send(text, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ---------- отправка анкеты в канал анкет ----------

async def submit_application(
    interaction: discord.Interaction, kind: str, title: str, fields: list[tuple[str, str, bool]], nickname: str
) -> None:
    """Публикует анкету в канале анкет с кнопками «Принять» / «Отклонить»."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    user = interaction.user
    channel = interaction.client.get_channel(config.FORMS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(config.FORMS_CHANNEL_ID)
        except discord.HTTPException:
            log.error("Канал анкет %s не найден", config.FORMS_CHANNEL_ID)
            await interaction.followup.send(
                "Не удалось отправить анкету: канал анкет не найден. Сообщите офицерам.", ephemeral=True
            )
            return

    embed = discord.Embed(
        title=title,
        description=f"{user.mention} — `{user}`",
        color=COLOR_PENDING,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value or "—", inline=inline)
    embed.add_field(name=STATUS_FIELD, value=STATUS_PENDING, inline=False)
    embed.set_footer(text=f"ID заявителя: {user.id}")

    try:
        message = await channel.send(embed=embed, view=ReviewView())
    except discord.Forbidden:
        log.error("Нет прав писать в канал анкет")
        await interaction.followup.send(
            "Не удалось отправить анкету: у бота нет прав в канале анкет. Сообщите офицерам.", ephemeral=True
        )
        return

    data = load_data()
    data[str(user.id)] = {
        "status": "pending",
        "kind": kind,
        "message_id": message.id,
        "nickname": nickname,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    save_data(data)
    log.info("Новая заявка (%s) от %s (%s)", kind, user, user.id)
    await delete_greeting(interaction.client, user.id)

    await interaction.followup.send(
        "✅ Анкета отправлена! Офицеры Легиона рассмотрят её и ответят вам в личные сообщения. "
        "Пожалуйста, не закрывайте личку от участников сервера.",
        ephemeral=True,
    )


# ---------- анкета новичка ----------

class ApplicationModal(discord.ui.Modal, title="Заявка в Легион"):
    nickname = discord.ui.TextInput(label="Ник в Foxhole", max_length=64)
    online = discord.ui.TextInput(
        label="Часовой пояс и время онлайна",
        placeholder="Например: МСК, по вечерам 19:00–23:00",
        max_length=100,
    )
    experience = discord.ui.TextInput(
        label="Опыт в Foxhole",
        style=discord.TextStyle.paragraph,
        placeholder="Сколько часов, в каких полках/кланах были, кем любите играть",
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await submit_application(
            interaction,
            kind="member",
            title="📜 Новая заявка в Легион",
            fields=[
                ("Ник в Foxhole", self.nickname.value, True),
                ("Онлайн", self.online.value, True),
                ("Опыт", self.experience.value, False),
            ],
            nickname=self.nickname.value,
        )


class AllyModal(discord.ui.Modal, title="Заявка союзника"):
    nickname = discord.ui.TextInput(label="Ник в Foxhole", max_length=64)
    unit = discord.ui.TextInput(
        label="Ваш полк / клан",
        placeholder="Название и тег",
        max_length=100,
    )
    position = discord.ui.TextInput(
        label="Ваша должность в нём",
        placeholder="Например: офицер, логист, рядовой",
        max_length=100,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await submit_application(
            interaction,
            kind="ally",
            title="🤝 Заявка союзника",
            fields=[
                ("Ник в Foxhole", self.nickname.value, True),
                ("Полк / клан", self.unit.value, True),
                ("Должность", self.position.value, True),
            ],
            nickname=self.nickname.value,
        )


class WardenModal(discord.ui.Modal, title="Заявка вардена"):
    nickname = discord.ui.TextInput(label="Ник в Foxhole", max_length=64)
    unit = discord.ui.TextInput(
        label="Ваш полк / клан",
        placeholder="Название и тег",
        max_length=100,
        required=False,
    )
    purpose = discord.ui.TextInput(
        label="Цель визита на сервер",
        style=discord.TextStyle.paragraph,
        placeholder="Переговоры, совместные ивенты, обмен…",
        max_length=500,
    )
    contact = discord.ui.TextInput(
        label="Кто из Легиона вас пригласил",
        max_length=200,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await submit_application(
            interaction,
            kind="warden",
            title="🛡️ Заявка вардена",
            fields=[
                ("Ник в Foxhole", self.nickname.value, True),
                ("Полк / клан", self.unit.value, True),
                ("Цель визита", self.purpose.value, False),
                ("Пригласил", self.contact.value, False),
            ],
            nickname=self.nickname.value,
        )


# Тип заявки → (ID роли, название ключа в .env, сообщение принятому)
def role_for(kind: str) -> tuple[int, str]:
    if kind == "ally":
        return config.ALLY_ROLE_ID, "ALLY_ROLE_ID"
    if kind == "warden":
        return config.WARDEN_ROLE_ID, "WARDEN_ROLE_ID"
    return config.MEMBER_ROLE_ID, "MEMBER_ROLE_ID"


WELCOME_TEXT = {
    "member": "🦅 Ave! Ваша заявка в Легион принята. Добро пожаловать в ряды, {name}!",
    "ally": "🤝 Ave, {name}! Легион признаёт вас союзником. Рады видеть вас на нашем сервере.",
    "warden": "🛡️ {name}, ваша заявка принята. Вам открыт канал для варденов на сервере Легиона.",
}


class RegistrationView(discord.ui.View):
    """Кнопка на панели в «Регистратуме». Работает и после перезапуска бота."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    async def _can_apply(interaction: discord.Interaction) -> bool:
        member = interaction.user
        role_ids = {r.id for r in getattr(member, "roles", [])}
        if config.MEMBER_ROLE_ID in role_ids:
            await interaction.response.send_message("Вы уже состоите в Легионе. 🦅", ephemeral=True)
            return False
        if config.ALLY_ROLE_ID and config.ALLY_ROLE_ID in role_ids:
            await interaction.response.send_message("Вы уже союзник Легиона. 🤝", ephemeral=True)
            return False
        if config.WARDEN_ROLE_ID and config.WARDEN_ROLE_ID in role_ids:
            await interaction.response.send_message("Вы уже получили роль варденов. 🛡️", ephemeral=True)
            return False

        app = load_data().get(str(member.id))
        if app and app.get("status") == "pending":
            await interaction.response.send_message(
                "Ваша заявка уже на рассмотрении. Дождитесь ответа офицеров.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Вступить в Легион", emoji="📝", style=discord.ButtonStyle.primary, custom_id="prok:apply"
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._can_apply(interaction):
            await interaction.response.send_modal(ApplicationModal())

    @discord.ui.button(
        label="Я союзник", emoji="🤝", style=discord.ButtonStyle.secondary, custom_id="prok:ally"
    )
    async def ally(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not config.ALLY_ROLE_ID:
            await interaction.response.send_message(
                "Приём союзников пока не настроен. Обратитесь к офицерам.", ephemeral=True
            )
            return
        if await self._can_apply(interaction):
            await interaction.response.send_modal(AllyModal())

    @discord.ui.button(
        label="Я варден", emoji="🛡️", style=discord.ButtonStyle.secondary, custom_id="prok:warden"
    )
    async def warden(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not config.WARDEN_ROLE_ID:
            await interaction.response.send_message(
                "Приём варденов пока не настроен. Обратитесь к офицерам.", ephemeral=True
            )
            return
        if await self._can_apply(interaction):
            await interaction.response.send_modal(WardenModal())


# ---------- рассмотрение заявки ----------

class RejectModal(discord.ui.Modal, title="Отклонение заявки"):
    reason = discord.ui.TextInput(
        label="Причина (её увидит заявитель)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, message: discord.Message, applicant_id: int) -> None:
        super().__init__()
        self.message = message
        self.applicant_id = applicant_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = load_data()
        app = data.get(str(self.applicant_id))
        if not app or app.get("status") != "pending":
            await interaction.followup.send("Эта заявка уже рассмотрена.", ephemeral=True)
            return

        reason = self.reason.value.strip() or "не указана"
        app["status"] = "rejected"
        app["reviewed_by"] = interaction.user.id
        app["reason"] = reason
        save_data(data)

        embed = set_status(
            self.message.embeds[0],
            f"❌ Отклонена — {interaction.user.mention}\nПричина: {reason}",
            COLOR_REJECTED,
        )
        await self.message.edit(embed=embed, view=None)

        delivered = False
        try:
            user = await interaction.client.fetch_user(self.applicant_id)
            delivered = await notify(
                user,
                f"К сожалению, ваша заявка отклонена.\nПричина: {reason}",
            )
        except discord.HTTPException:
            pass

        note = "" if delivered else " Заявитель не получил уведомление: личка закрыта."
        await interaction.followup.send(f"Заявка отклонена.{note}", ephemeral=True)
        log.info("Заявка %s отклонена (%s)", self.applicant_id, interaction.user)


class ReviewView(discord.ui.View):
    """Кнопки под анкетой в канале анкет. Работают и после перезапуска бота."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _get_pending(self, interaction: discord.Interaction) -> int | None:
        if not isinstance(interaction.user, discord.Member) or not is_reviewer(interaction.user):
            await interaction.response.send_message(
                "Рассматривать заявки могут только офицеры Легиона.", ephemeral=True
            )
            return None

        user_id, app = find_by_message(load_data(), interaction.message.id)
        if user_id is None:
            await interaction.response.send_message("Заявка не найдена в базе бота.", ephemeral=True)
            return None
        if app.get("status") != "pending":
            await interaction.response.send_message("Эта заявка уже рассмотрена.", ephemeral=True)
            return None
        return int(user_id)

    @discord.ui.button(label="Принять", emoji="✅", style=discord.ButtonStyle.success, custom_id="prok:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        applicant_id = await self._get_pending(interaction)
        if applicant_id is None:
            return
        await interaction.response.defer()

        guild = interaction.guild
        data = load_data()
        app = data[str(applicant_id)]

        try:
            member = await guild.fetch_member(applicant_id)
        except discord.NotFound:
            app["status"] = "left"
            save_data(data)
            embed = set_status(interaction.message.embeds[0], "🚪 Заявитель покинул сервер", COLOR_REJECTED)
            await interaction.edit_original_response(embed=embed, view=None)
            return

        kind = app.get("kind", "member")
        role_id, key = role_for(kind)
        member_role = guild.get_role(role_id) if role_id else None
        if member_role is None:
            await interaction.followup.send(f"Роль не найдена. Проверьте {key} в .env.", ephemeral=True)
            return

        try:
            await member.add_roles(member_role, reason=f"Заявка принята: {interaction.user}")
            if config.NEWCOMER_ROLE_ID:
                newcomer = guild.get_role(config.NEWCOMER_ROLE_ID)
                if newcomer and newcomer in member.roles:
                    await member.remove_roles(newcomer, reason="Заявка принята")
        except discord.Forbidden:
            await interaction.followup.send(
                "У бота не хватает прав выдать роль. В настройках сервера роль бота должна стоять "
                "выше выдаваемых ролей, и у бота должно быть право «Управлять ролями».",
                ephemeral=True,
            )
            return

        app["status"] = "approved"
        app["reviewed_by"] = interaction.user.id
        save_data(data)

        welcome = WELCOME_TEXT.get(kind, WELCOME_TEXT["member"]).format(name=member.display_name)
        delivered = await notify(member, welcome)
        status = f"✅ Принята — {interaction.user.mention}"
        if not delivered:
            status += "\n(личка заявителя закрыта, уведомление не доставлено)"

        embed = set_status(interaction.message.embeds[0], status, COLOR_APPROVED)
        await interaction.edit_original_response(embed=embed, view=None)
        log.info("Заявка %s принята (%s)", applicant_id, interaction.user)

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger, custom_id="prok:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        applicant_id = await self._get_pending(interaction)
        if applicant_id is None:
            return
        await interaction.response.send_modal(RejectModal(interaction.message, applicant_id))


# ---------- команды ----------

class Registration(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Приветствует нового участника в Регистратуме и предлагает подать заявку."""
        if member.bot or member.guild.id != config.GUILD_ID:
            return
        if config.NEWCOMER_ROLE_ID:
            pleb = member.guild.get_role(config.NEWCOMER_ROLE_ID)
            if pleb is None:
                log.error("Роль «Плебей» %s не найдена", config.NEWCOMER_ROLE_ID)
            else:
                try:
                    await member.add_roles(pleb, reason="Новоприбывший")
                except discord.HTTPException as e:
                    log.error("Не удалось выдать «Плебея» %s: %s", member, e)

        # Личное сообщение со ссылкой на Регистратум
        link = f"https://discord.com/channels/{member.guild.id}/{config.REGISTRATUM_CHANNEL_ID}"
        dm_view = discord.ui.View()
        dm_view.add_item(discord.ui.Button(label="Перейти в Регистратум", emoji="🏛️", url=link))
        if not await notify_with_view(
            member,
            "**AVE!** До прохождения регистрации тебе присвоен статус **Плебей**.\n\n"
            f"Посети канал <#{config.REGISTRATUM_CHANNEL_ID}>, чтобы подать заявку "
            "и получить соответствующий статус.\n\n"
            "*Senatus Populusque Legionis*",
            dm_view,
        ):
            log.info("Не удалось написать в личку %s: личка закрыта", member)

        channel = member.guild.get_channel(config.REGISTRATUM_CHANNEL_ID)
        if channel is None:
            log.error("Канал Регистратума %s не найден", config.REGISTRATUM_CHANNEL_ID)
            return
        embed = discord.Embed(
            title="🏛️ Ave, путник!",
            description=(
                f"Добро пожаловать к воротам Легиона, {member.mention}.\n\n"
                "📝 Хотите в наши ряды — нажмите «Вступить в Легион».\n"
                "🤝 Если вы из союзного полка, нажмите «Я союзник».\n"
                "🛡️ Если вы играете за варденов, нажмите «Я варден».\n\n"
                "Офицеры рассмотрят анкету и ответят вам в личные сообщения."
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            greeting = await channel.send(
                content=member.mention,
                embed=embed,
                view=RegistrationView(),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            remember_greeting(member.id, greeting)
            log.info("Поприветствовал новичка %s (%s)", member, member.id)
        except discord.HTTPException as e:
            log.error("Не удалось поприветствовать %s: %s", member, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Человек ушёл с сервера — убираем его приветствие."""
        if member.guild.id == config.GUILD_ID:
            await delete_greeting(self.bot, member.id)

    @app_commands.command(name="регистратум", description="Опубликовать панель подачи заявок в Регистратуме")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.guild.get_channel(config.REGISTRATUM_CHANNEL_ID) or interaction.channel
        embed = discord.Embed(
            title="🏛️ РЕГИСТРАТУМ ЛЕГИОНА",
            description=(
                "AVE! Приветствуем тебя!\n\n"
                "⚔️ Кто ты, воин?\n\n"
                "📜 Желаешь вступить в ряды Легиона? Нажми «Вступить в Легион» и заполни анкету.\n\n"
                "🤝 Прибыл из союзного полка? Нажми «Я союзник», чтобы получить соответствующий статус.\n\n"
                "🛡️ Сражаешься на стороне варденов? Нажми «Я варден».\n\n"
                "Офицерский состав рассмотрит твою анкету и свяжется с тобой в личных сообщениях.\n\n"
                "*Senatus Populusque Legionis*"
            ),
            color=discord.Color.dark_red(),
        )
        try:
            panel = await channel.send(embed=embed, view=RegistrationView())
        except discord.Forbidden:
            await interaction.followup.send(
                f"У бота нет прав писать в {channel.mention}. Дайте роли бота в этом канале права "
                "«Просматривать канал», «Отправлять сообщения» и «Встраивать ссылки».",
                ephemeral=True,
            )
            return
        try:
            await panel.pin(reason="Панель Регистратума")
            note = "и закреплена"
        except discord.HTTPException:
            note = (
                "но закрепить её не вышло: дайте боту право «Закреплять сообщения» "
                "(или «Управлять сообщениями») либо закрепите вручную"
            )
        await interaction.followup.send(f"✅ Панель опубликована в {channel.mention} {note}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RegistrationView())
    bot.add_view(ReviewView())
    await bot.add_cog(Registration(bot))
