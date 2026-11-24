"""Выбор дня и времени из меню — вместо ввода текстом.

add_when(view, mode, row_day, row_time) добавляет во view два меню: «День» и «Время».
Режимы:
  "start"    — начало события (операция, сбор): «через 15 мин»… и часы;
  "publish"  — публикация (эдикт): «Сразу» по умолчанию, либо день и время;
  "deadline" — срок (поручение, заказ, задание): «Без срока» по умолчанию, «конец дня» и часы.
Время — в часовом поясе TZ_OFFSET; Discord показывает каждому в его поясе.
Обработчик выбора вызывает view.on_when(interaction) — view сам перерисовывает сообщение.
"""
import time
from datetime import datetime, timedelta
from typing import Optional

import discord

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _tz():
    from cogs.operations import local_tz
    return local_tz()


def _tz_name() -> str:
    from cogs.operations import tz_name
    return tz_name()


def day_options(mode: str) -> list[discord.SelectOption]:
    today = datetime.now(_tz())
    opts = []
    if mode == "deadline":
        opts.append(discord.SelectOption(label="Без срока", value="none", emoji="♾️", default=True))
    if mode == "publish":
        opts.append(discord.SelectOption(label="Сразу", value="now", emoji="⚡", default=True))
    for i in range(7):
        d = today + timedelta(days=i)
        name = {0: "Сегодня", 1: "Завтра"}.get(i, WEEKDAYS[d.weekday()])
        opts.append(discord.SelectOption(label=f"{name} · {d:%d.%m}", value=str(i),
                                         default=(mode == "start" and i == 0)))
    return opts


def time_options(mode: str) -> list[discord.SelectOption]:
    opts = []
    if mode in ("start", "publish"):
        for minutes, label in ((15, "Через 15 минут"), (30, "Через 30 минут"), (60, "Через 1 час"),
                               (120, "Через 2 часа")):
            opts.append(discord.SelectOption(label=label, value=f"+{minutes}", emoji="⏱️"))
        hours = range(10, 24)
    else:
        opts.append(discord.SelectOption(label="Конец дня (23:59)", value="23:59", emoji="🌙"))
        for minutes, label in ((60, "Через 1 час"), (180, "Через 3 часа"), (360, "Через 6 часов")):
            opts.append(discord.SelectOption(label=label, value=f"+{minutes}", emoji="⏱️"))
        hours = range(8, 24)
    for h in hours:
        opts.append(discord.SelectOption(label=f"{h}:00", value=f"{h:02d}:00"))
        if mode != "deadline" and h in (19, 20, 21):
            opts.append(discord.SelectOption(label=f"{h}:30", value=f"{h:02d}:30"))
    return opts[:25]


def add_when(view: discord.ui.View, mode: str, row_day: int, row_time: int) -> None:
    """Добавляет во view меню дня и времени. Состояние — view.when_day / view.when_time."""
    view.when_mode = mode
    view.when_day = {"start": "0", "publish": "now", "deadline": "none"}[mode]
    view.when_time = None

    day = discord.ui.Select(placeholder="День", options=day_options(mode), row=row_day)
    tm = discord.ui.Select(placeholder=f"Время ({_tz_name()})", options=time_options(mode), row=row_time)

    async def on_day(interaction: discord.Interaction) -> None:
        view.when_day = day.values[0]
        for o in day.options:
            o.default = o.value == view.when_day
        await view.on_when(interaction)

    async def on_time(interaction: discord.Interaction) -> None:
        view.when_time = tm.values[0]
        for o in tm.options:
            o.default = o.value == view.when_time
        if view.when_day in ("none", "now") and not view.when_time.startswith("+"):
            view.when_day = "0"  # выбрали час — значит сегодня
            for o in day.options:
                o.default = o.value == "0"
        await view.on_when(interaction)

    day.callback = on_day
    tm.callback = on_time
    view.add_item(day)
    view.add_item(tm)


def when_ts(view: discord.ui.View) -> Optional[int]:
    """Выбранное время (unix) или None — «без срока» / «сразу» / ещё не выбрано."""
    t = getattr(view, "when_time", None)
    d = getattr(view, "when_day", None)
    now = datetime.now(_tz())
    if t and t.startswith("+"):
        return int(now.timestamp()) + int(t[1:]) * 60
    if d in (None, "none", "now"):
        return None
    if not t:
        if view.when_mode == "deadline":  # выбран только день — до конца дня
            t = "23:59"
        else:
            return None
    h, m = map(int, t.split(":"))
    moment = (now + timedelta(days=int(d))).replace(hour=h, minute=m, second=0, microsecond=0)
    return int(moment.timestamp())


def _need_time(view) -> bool:
    """Выбран день, но не час (для начала и публикации)."""
    return (view.when_mode != "deadline" and view.when_day not in ("none", "now")
            and when_ts(view) is None)


def when_text(view: discord.ui.View) -> str:
    ts = when_ts(view)
    mode = view.when_mode
    if _need_time(view):
        return "выберите время"
    if ts is None:
        if mode == "deadline":
            return "без срока"
        if mode == "publish":
            return "сразу"
        return "выберите время"
    text = f"<t:{ts}:F> (<t:{ts}:R>)"
    if ts <= time.time():
        text += " ⚠️ уже прошло"
    return text


def when_ok(view: discord.ui.View) -> Optional[str]:
    """Ошибка выбора или None, если всё в порядке."""
    ts = when_ts(view)
    if _need_time(view):
        return "Выберите время."
    if ts is not None and ts <= time.time():
        return "Выбранное время уже прошло — выберите другое."
    return None
