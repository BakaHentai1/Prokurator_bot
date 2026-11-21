"""Резервные копии данных бота в канал «Инвентариум».

Раз в сутки (и по команде /бэкап) бот упаковывает папку data — базу складов,
анкеты и служебные файлы — в zip и отправляет в канал AUDIT_CHANNEL_ID.

ВНИМАНИЕ: в копии есть пароли складов. Канал должны видеть только доверенные.

Восстановление: остановить бота, распаковать архив в папку data, запустить бота.
"""
import io
import json
import logging
import sqlite3
import time
import zipfile
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger("backup")

BACKUP_EVERY_HOURS = 24
STATE_FILE = config.DATA_DIR / "backup_state.json"


def last_backup() -> float:
    try:
        return float(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last", 0))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return 0.0


def set_last_backup(ts: float) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last": ts}), encoding="utf-8")


def build_archive() -> tuple[io.BytesIO, list[str]]:
    """Упаковывает папку data в zip в памяти. Базы SQLite копируются безопасно."""
    buffer = io.BytesIO()
    names = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(config.DATA_DIR.glob("*")):
            if not path.is_file() or path.name == STATE_FILE.name:
                continue
            if path.suffix == ".db":
                # Копия через backup API — целая, даже если бот сейчас пишет в базу
                src = sqlite3.connect(path)
                mem = sqlite3.connect(":memory:")
                src.backup(mem)
                src.close()
                data = mem.serialize()
                mem.close()
            else:
                data = path.read_bytes()
            zf.writestr(path.name, data)
            names.append(path.name)
    buffer.seek(0)
    return buffer, names


async def send_backup(client: discord.Client, reason: str) -> str:
    """Отправляет копию в Инвентариум. Возвращает текст результата."""
    channel = client.get_channel(config.AUDIT_CHANNEL_ID) or await client.fetch_channel(config.AUDIT_CHANNEL_ID)
    buffer, names = build_archive()
    if not names:
        return "Данных для копии пока нет."
    size_kb = len(buffer.getbuffer()) / 1024
    filename = f"legion_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.zip"
    now = int(time.time())
    await channel.send(
        f"💾 **Резервная копия** · <t:{now}:f> · {reason}\n"
        f"Файлы: {', '.join(names)} · {size_kb:.1f} КБ\n"
        "🔒 Содержит пароли складов — не пересылайте.",
        file=discord.File(buffer, filename=filename),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    set_last_backup(now)
    log.info("Резервная копия отправлена (%s, %.1f КБ)", reason, size_kb)
    return f"✅ Копия отправлена в {channel.mention}."


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.daily.start()

    async def cog_unload(self) -> None:
        self.daily.cancel()

    @tasks.loop(minutes=30)
    async def daily(self) -> None:
        if time.time() - last_backup() < BACKUP_EVERY_HOURS * 3600:
            return
        try:
            await send_backup(self.bot, "ежедневная")
        except Exception:
            log.exception("Не удалось сделать резервную копию")

    @daily.before_loop
    async def before_daily(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="бэкап", description="Сделать резервную копию данных бота в Инвентариум")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def backup_now(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⛔ Только для администраторов.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await send_backup(self.bot, f"вручную: {interaction.user}")
        except discord.HTTPException as e:
            result = f"Не удалось отправить копию: {e.text or e}"
        await interaction.followup.send(result, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    if not config.AUDIT_CHANNEL_ID:
        log.warning("AUDIT_CHANNEL_ID (Инвентариум) не задан — резервные копии выключены. Запустите: python setup.py")
        return
    await bot.add_cog(Backup(bot))
