"""Уборщик: чтобы в каналах не копился мусор.

1. Ответы «Только вы видите это сообщение» удаляются сами:
   • простые ответы — через 3 минуты;
   • окна с кнопками и меню (настройка операции, эдикта и т. п.) — через 14 минут
     (дольше нельзя: Discord даёт боту 15 минут на такие сообщения);
   • когда окно закончило работу (кнопки исчезли) — через 3 минуты.
2. Временные объявления в каналах (напоминания, «не явился», «MPF ждёт» и т. п.)
   ставятся в очередь через later() и удаляются в срок — очередь переживает перезапуск.
"""
import asyncio
import json
import logging
import time

import discord
from discord.ext import commands, tasks

import config

log = logging.getLogger("janitor")

SHORT = 180          # простой ответ
LONG = 14 * 60       # окно с кнопками
QUEUE = config.DATA_DIR / "cleanup.json"


# ---------- 1. скрытые ответы ----------

def _has_view(kwargs: dict) -> bool:
    view = kwargs.get("view")
    return view is not None and view is not discord.utils.MISSING and len(getattr(view, "children", [])) > 0


_orig_send = discord.InteractionResponse.send_message
_orig_edit = discord.InteractionResponse.edit_message
_orig_defer = discord.InteractionResponse.defer
_orig_webhook_send = discord.Webhook.send


async def _send_message(self, *args, **kwargs):
    if kwargs.get("ephemeral") and kwargs.get("delete_after") is None:
        kwargs["delete_after"] = LONG if _has_view(kwargs) else SHORT
    return await _orig_send(self, *args, **kwargs)


async def _edit_message(self, *args, **kwargs):
    message = getattr(self._parent, "message", None)
    ephemeral = bool(message and message.flags.ephemeral)
    if ephemeral and kwargs.get("delete_after") is None and "view" in kwargs and not _has_view(kwargs):
        kwargs["delete_after"] = SHORT  # окно закончило работу
    return await _orig_edit(self, *args, **kwargs)


async def _drop_original(interaction: discord.Interaction, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


async def _defer(self, *args, **kwargs):
    result = await _orig_defer(self, *args, **kwargs)
    if kwargs.get("ephemeral") and kwargs.get("thinking"):
        asyncio.create_task(_drop_original(self._parent, LONG))
    return result


async def _webhook_send(self, *args, **kwargs):
    if not kwargs.get("ephemeral"):
        return await _orig_webhook_send(self, *args, **kwargs)
    asked_wait = kwargs.get("wait", False)
    kwargs["wait"] = True
    msg = await _orig_webhook_send(self, *args, **kwargs)
    try:
        delay = LONG if _has_view(kwargs) else SHORT
        asyncio.create_task(msg.delete(delay=delay))
    except Exception:
        pass
    return msg if asked_wait else None


def install() -> None:
    discord.InteractionResponse.send_message = _send_message
    discord.InteractionResponse.edit_message = _edit_message
    discord.InteractionResponse.defer = _defer
    discord.Webhook.send = _webhook_send


# ---------- 2. временные объявления ----------

def _load() -> list[list[int]]:
    try:
        return json.loads(QUEUE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[list[int]]) -> None:
    try:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE.write_text(json.dumps(items), encoding="utf-8")
    except OSError as e:
        log.warning("Очередь уборки не сохранена: %s", e)


def later(message: discord.Message | None, seconds: int) -> discord.Message | None:
    """Удалить сообщение в канале через seconds секунд (переживает перезапуск бота)."""
    if message is None:
        return None
    items = _load()
    items.append([message.channel.id, message.id, int(time.time()) + seconds])
    _save(items)
    return message


class Janitor(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()

    @tasks.loop(minutes=1)
    async def sweep(self) -> None:
        items = _load()
        if not items:
            return
        now = time.time()
        keep = []
        for cid, mid, when in items:
            if when > now:
                keep.append([cid, mid, when])
                continue
            channel = self.bot.get_channel(cid)
            if channel is None:
                continue
            try:
                await channel.get_partial_message(mid).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                if when > now - 86400:  # временная ошибка — попробуем позже, но не дольше суток
                    keep.append([cid, mid, when])
            await asyncio.sleep(0.5)
        _save(keep)

    @sweep.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


install()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Janitor(bot))
