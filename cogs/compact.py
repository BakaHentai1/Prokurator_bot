"""Компактные панели: одно меню вместо стены кнопок.

Каждая панель — выпадающее меню (и максимум одна главная кнопка), которое вызывает
те же действия, что и старые кнопки. Старые панели продолжают работать.
"""
import discord
from discord.ext import commands


def _call(old_view_cls, item_name: str):
    """Вызывает обработчик старой кнопки по имени."""
    async def run(interaction: discord.Interaction) -> None:
        view = old_view_cls()
        await getattr(view, item_name).callback(interaction)
    return run


class MenuPanel(discord.ui.View):
    """Базовая компактная панель: меню из (подпись, эмодзи, имя_обработчика) + необязательная кнопка."""

    old_cls = None
    menu_id = ""
    placeholder = "Выберите действие…"
    options: list[tuple[str, str, str]] = []
    button: tuple[str, str, str, str] | None = None  # (подпись, эмодзи, имя, custom_id)

    def __init__(self) -> None:
        super().__init__(timeout=None)
        select = discord.ui.Select(
            placeholder=self.placeholder, custom_id=self.menu_id,
            options=[discord.SelectOption(label=label, emoji=emoji, value=name) for label, emoji, name in self.options],
        )
        select.callback = self._on_select
        self.add_item(select)
        if self.button:
            label, emoji, name, custom_id = self.button
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.success, custom_id=custom_id)
            btn.callback = _call(self.old_cls, name)
            self.add_item(btn)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        name = interaction.data.get("values", [""])[0]
        await _call(self.old_cls, name)(interaction)


def consulate_panel() -> type[MenuPanel]:
    from cogs.consulate import ConsulatePanel

    class ConsulateMenu(MenuPanel):
        old_cls = ConsulatePanel
        menu_id = "cons:menu"
        placeholder = "🏛️ Консулат…"
        options = [
            ("Текущие голосования", "📋", "active"),
            ("Новая операция", "⚔️", "new_op"),
            ("Новое голосование", "🗳️", "new_poll"),
            ("Новый эдикт", "📜", "new_edict"),
            ("Выборы", "🏛️", "new_election"),
        ]
    return ConsulateMenu


def senate_panel() -> type[MenuPanel]:
    from cogs.senate import SenatePanel

    class SenateMenu(MenuPanel):
        old_cls = SenatePanel
        menu_id = "sen:menu"
        placeholder = "🏛️ Сенат…"
        options = [
            ("Личное дело", "📂", "dossier"),
            ("Чёрный список", "⛔", "blacklist"),
            ("Выговоры", "⚠️", "warnings"),
            ("Представление к повышению", "🎖️", "promotion"),
            ("Открытые представления", "📋", "promotions"),
            ("Новое поручение", "📌", "new_task"),
            ("Поручения", "🗂️", "task_list"),
            ("Кадры", "👥", "staff"),
            ("Доклад за неделю", "📊", "report"),
        ]
    return SenateMenu


def prefecture_panel() -> type[MenuPanel]:
    from cogs.prefecture import PrefecturePanel

    class PrefectureMenu(MenuPanel):
        old_cls = PrefecturePanel
        menu_id = "pref:menu"
        placeholder = "🏭 Префектура…"
        button = ("Поставил в MPF", "🏗️", "mpf_add", "pref:mpf2")
        options = [
            ("Открытые заказы", "📋", "open_list"),
            ("Мои заказы", "🙋", "mine"),
            ("Новый заказ (офицеры)", "➕", "new"),
            ("Очередь MPF", "⏳", "mpf_list"),
            ("Объекты", "🏭", "facilities"),
            ("Новый объект (офицеры)", "🔧", "facility_new"),
            ("Сбор на добычу (офицеры)", "⛏️", "gathering"),
            ("Ресурсы", "📦", "resources"),
        ]
    return PrefectureMenu


class Compact(commands.Cog):
    pass


async def setup(bot: commands.Bot) -> None:
    for factory in (consulate_panel, senate_panel, prefecture_panel):
        try:
            bot.add_view(factory()())
        except Exception:
            pass
    await bot.add_cog(Compact())
