"""Регистратум: анкета новичка и рассмотрение заявок офицерами.

Как это работает:
0. Когда человек заходит на сервер, бот приветствует его в «Регистратуме»
   и сразу прикладывает кнопку «Подать заявку».
1. Администратор пишет /регистратум — бот публикует панель с кнопкой «Подать заявку».
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
    source = discord.ui.TextInput(
        label="Откуда узнали о Легионе",
        max_length=200,
        required=False,
    )
    motivation = discord.ui.TextInput(
        label="Почему хотите вступить",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        channel = interaction.client.get_channel(config.FORMS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(config.FORMS_CHANNEL_ID)
            except discord.HTTPException:
                log.error("Канал анкет %s не найден", config.FORMS_CHANNEL_ID)
                await interaction.followup.send(
                    "Не удалось отправить анкету: канал анкет не найден. Сообщите офицерам.",
                    ephemeral=True,
                )
                return

        embed = discord.Embed(
            title="📜 Новая заявка в Легион",
            description=f"{user.mention} — `{user}`",
            color=COLOR_PENDING,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Ник в Foxhole", value=self.nickname.value, inline=True)
        embed.add_field(name="Онлайн", value=self.online.value, inline=True)
        embed.add_field(name="Опыт", value=self.experience.value, inline=False)
        embed.add_field(name="Откуда узнал", value=self.source.value or "—", inline=False)
        embed.add_field(name="Почему к нам", value=self.motivation.value, inline=False)
        embed.add_field(name=STATUS_FIELD, value=STATUS_PENDING, inline=False)
        embed.set_footer(text=f"ID заявителя: {user.id}")

        try:
            message = await channel.send(embed=embed, view=ReviewView())
        except discord.Forbidden:
            log.error("Нет прав писать в канал анкет")
            await interaction.followup.send(
                "Не удалось отправить анкету: у бота нет прав в канале анкет. Сообщите офицерам.",
                ephemeral=True,
            )
            return

        data = load_data()
        data[str(user.id)] = {
            "status": "pending",
            "message_id": message.id,
            "nickname": self.nickname.value,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        save_data(data)
        log.info("Новая заявка от %s (%s)", user, user.id)

        await interaction.followup.send(
            "✅ Анкета отправлена! Офицеры Легиона рассмотрят её и ответят вам в личные сообщения. "
            "Пожалуйста, не закрывайте личку от участников сервера.",
            ephemeral=True,
        )


class RegistrationView(discord.ui.View):
    """Кнопка на панели в «Регистратуме». Работает и после перезапуска бота."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Подать заявку", emoji="📝", style=discord.ButtonStyle.primary, custom_id="prok:apply"
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if isinstance(member, discord.Member) and any(r.id == config.MEMBER_ROLE_ID for r in member.roles):
            await interaction.response.send_message("Вы уже состоите в Легионе. 🦅", ephemeral=True)
            return

        app = load_data().get(str(member.id))
        if app and app.get("status") == "pending":
            await interaction.response.send_message(
                "Ваша заявка уже на рассмотрении. Дождитесь ответа офицеров.", ephemeral=True
            )
            return

        await interaction.response.send_modal(ApplicationModal())


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
                f"К сожалению, ваша заявка в Легион отклонена.\nПричина: {reason}",
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

        member_role = guild.get_role(config.MEMBER_ROLE_ID)
        if member_role is None:
            await interaction.followup.send(
                "Роль легионера не найдена. Проверьте MEMBER_ROLE_ID в .env.", ephemeral=True
            )
            return

        try:
            await member.add_roles(member_role, reason=f"Заявка принята: {interaction.user}")
            if config.NEWCOMER_ROLE_ID:
                newcomer = guild.get_role(config.NEWCOMER_ROLE_ID)
                if newcomer and newcomer in member.roles:
                    await member.remove_roles(newcomer, reason="Принят в Легион")
        except discord.Forbidden:
            await interaction.followup.send(
                "У бота не хватает прав выдать роль. В настройках сервера роль бота должна стоять "
                "выше роли легионера, и у бота должно быть право «Управлять ролями».",
                ephemeral=True,
            )
            return

        app["status"] = "approved"
        app["reviewed_by"] = interaction.user.id
        save_data(data)

        delivered = await notify(
            member,
            f"🦅 Ave! Ваша заявка в Легион принята. Добро пожаловать в ряды, {member.display_name}!",
        )
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
        channel = member.guild.get_channel(config.REGISTRATUM_CHANNEL_ID)
        if channel is None:
            log.error("Канал Регистратума %s не найден", config.REGISTRATUM_CHANNEL_ID)
            return
        embed = discord.Embed(
            title="🏛️ Ave, путник!",
            description=(
                f"Добро пожаловать к воротам Легиона, {member.mention}.\n\n"
                "Чтобы вступить в наши ряды, нажмите кнопку ниже и заполните короткую анкету. "
                "Офицеры рассмотрят её и ответят вам в личные сообщения."
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(
                content=member.mention,
                embed=embed,
                view=RegistrationView(),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            log.info("Поприветствовал новичка %s (%s)", member, member.id)
        except discord.HTTPException as e:
            log.error("Не удалось поприветствовать %s: %s", member, e)

    @app_commands.command(name="регистратум", description="Опубликовать панель подачи заявок в Регистратуме")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def post_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.guild.get_channel(config.REGISTRATUM_CHANNEL_ID) or interaction.channel
        embed = discord.Embed(
            title="🏛️ Регистратум Легиона",
            description=(
                "Приветствуем, путник!\n\n"
                "Чтобы вступить в Легион, нажмите кнопку ниже и заполните короткую анкету. "
                "Офицеры рассмотрят её и ответят вам в личные сообщения.\n\n"
                "*Senatus Populusque Legionis*"
            ),
            color=discord.Color.dark_red(),
        )
        try:
            await channel.send(embed=embed, view=RegistrationView())
        except discord.Forbidden:
            await interaction.followup.send(
                f"У бота нет прав писать в {channel.mention}. Дайте роли бота в этом канале права "
                "«Просматривать канал», «Отправлять сообщения» и «Встраивать ссылки».",
                ephemeral=True,
            )
            return
        await interaction.followup.send(f"Панель опубликована в {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RegistrationView())
    bot.add_view(ReviewView())
    await bot.add_cog(Registration(bot))
