"""/помощь — что умеет Прокуратор, с учётом ролей того, кто спрашивает."""
import discord
from discord import app_commands
from discord.ext import commands

import config


def channel(cid: int, fallback: str) -> str:
    return f"<#{cid}>" if cid else fallback


def build_help(member: discord.abc.User) -> discord.Embed:
    from cogs import consulate, operations, registration
    from cogs import warehouses

    admin = consulate.is_admin(member)
    officer = admin or consulate.has_any(member, consulate.officer_roles())
    command_staff = consulate.can_create(member)          # Принцепс, Консулы, Сенат
    reviewer = isinstance(member, discord.Member) and registration.is_reviewer(member)
    wh_on = warehouses.db is not None

    def wh(action: str) -> bool:
        return wh_on and warehouses.has(member, action)

    e = discord.Embed(
        title="🏛️ ПРОКУРАТОР · ПОМОЩЬ",
        description="Секретарь Легиона. Ниже — то, что доступно **вам**.",
        color=discord.Color.dark_red(),
    )

    reg = [
        f"Новички подают заявки кнопками в {channel(config.REGISTRATUM_CHANNEL_ID, '«Регистратуме»')}: "
        "«Вступить в Легион», «Я союзник», «Я варден». Ответ приходит в личные сообщения."
    ]
    if reviewer:
        reg.append(f"🎖️ Вы рассматриваете заявки в {channel(config.FORMS_CHANNEL_ID, 'канале анкет')} "
                   "кнопками «Принять» / «Отклонить».")
    if admin:
        reg.append("⚙️ `/регистратум` — опубликовать и закрепить панель Регистратума.")
    e.add_field(name="📝 Регистратум", value="\n".join(reg), inline=False)

    if wh_on:
        lines = []
        if wh("search"):
            lines.append(f"Панель в {channel(config.SECRETARIUM_CHANNEL_ID, '«Секретариуме»')}: "
                         "«Все склады», «Мои склады». `/склад найти` — поиск по названию, гексу, региону, владельцу.")
        if wh("create"):
            lines.append("➕ `/склад добавить` — зарегистрировать склад (или кнопка на панели).")
        if wh("password"):
            pw = "всех складов, включая офицерские" if wh("officer") else "общих складов"
            lines.append(f"🔑 Кнопка «Пароль» на карточке — пароль {pw} придёт в личку.")
        lines.append("👤 Владелец склада может менять его данные и пароль; пароль личного склада — только у владельца.")
        lines.append("♻️ После обновления склада в игре жмите «Обновил» — бот напомнит за 12 ч и за 1 ч до удаления.")
        if wh("audit"):
            lines.append("📜 `/склад журнал` — история действий со складом.")
        if wh("perms"):
            lines.append("⚙️ `/склад панель`, `/склад права`, `/склад выдать_право`, `/склад снять_право`, "
                         "`/склад сбросить_права`.")
        e.add_field(name="📦 Секретариум · склады", value="\n".join(lines), inline=False)

    cons = [
        "🗳️ Голосуйте кнопками под голосованием — голос можно менять до конца.",
        "⚔️ Отмечайтесь на операциях: «Приду», «Опоздаю», «Не приду». За 30 минут бот напомнит "
        f"в {channel(config.LEGION_CHAT_CHANNEL_ID, 'чате Легиона')}.",
        "📜 Жмите «Принято» под эдиктами.",
    ]
    if officer:
        cons.append("⚔️ `/операция` — объявить операцию (или кнопка на панели Консулата). "
                    "👁️ «Кто ознакомился» под эдиктами.")
    if command_staff:
        cons.append("🗳️ `/голосование` — тайное или открытое, итоги публикуются сами.")
        cons.append("📜 `/эдикт`, `/эдикт_изменить`, `/эдикт_отменить` — эдикты, можно по расписанию.")
    if admin:
        cons.append("⚙️ `/консулат` — опубликовать и закрепить панель Консулата.")
    e.add_field(name="🏛️ Консулат", value="\n".join(cons), inline=False)

    if admin:
        e.add_field(
            name="⚙️ Служебное",
            value=f"💾 `/бэкап` — резервная копия в {channel(config.AUDIT_CHANNEL_ID, '«Инвентариум»')} "
                  "(делается и сама раз в сутки).\n"
                  f"📜 Журнал складов — {channel(config.AUDIT_CHANNEL_ID, '«Инвентариум»')}, "
                  f"журнал Консулата — {channel(config.ARCHIVE_CHANNEL_ID, '«Архивариум»')}.",
            inline=False,
        )

    e.set_footer(text="Показаны возможности для ваших ролей · Senatus Populusque Legionis")
    return e


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="помощь", description="Что умеет Прокуратор")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_help(interaction.user), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
