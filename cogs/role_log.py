"""Журнал смены ролей в «Архивариум»: кто, кому, какие роли выдал или снял.

Берёт данные из журнала аудита сервера, поэтому боту нужно право
«Просматривать журнал аудита». Смены ролей самим ботом (по заявкам, выборам)
тоже записываются — с причиной. Смены званий сохраняются для истории в профиле.
"""
import logging
import time

import discord
from discord.ext import commands

import config

log = logging.getLogger("role_log")


def roles_text(roles) -> str:
    return ", ".join(f"<@&{r.id}>" for r in roles)


class RoleLog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if entry.action != discord.AuditLogAction.member_role_update:
            return
        if entry.guild.id != config.GUILD_ID:
            return
        added = list(getattr(entry.changes.after, "roles", None) or [])
        removed = list(getattr(entry.changes.before, "roles", None) or [])
        if not added and not removed:
            return

        target = f"<@{entry.target.id}>" if entry.target else "кому-то"
        if entry.user_id == self.bot.user.id:
            who = "🤖 Прокуратор"
        elif entry.user_id:
            who = f"<@{entry.user_id}>"
        else:
            who = "Кто-то"

        lines = [f"🎭 <t:{int(time.time())}:f> · {who} изменил роли {target}"]
        if added:
            lines.append(f"➕ выдал: {roles_text(added)}")
        if removed:
            lines.append(f"➖ снял: {roles_text(removed)}")
        if entry.reason:
            lines.append(f"📝 причина: {entry.reason}")

        # История званий для профиля
        try:
            from cogs import profiles
            if profiles.db is not None and entry.target:
                for r in added:
                    profiles.db.add_role_event(entry.target.id, r.id, "add", entry.user_id)
                for r in removed:
                    profiles.db.add_role_event(entry.target.id, r.id, "remove", entry.user_id)
        except Exception:
            log.exception("Не удалось записать историю званий")

        channel = self.bot.get_channel(config.ARCHIVE_CHANNEL_ID)
        if channel is None:
            return
        try:
            await channel.send("\n".join(lines)[:2000], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.error("Не удалось записать смену ролей: %s", e)


async def setup(bot: commands.Bot) -> None:
    if not config.ARCHIVE_CHANNEL_ID:
        log.warning("ARCHIVE_CHANNEL_ID не задан — журнал смены ролей выключен")
        return
    await bot.add_cog(RoleLog(bot))
