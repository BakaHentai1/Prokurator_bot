"""Прокуратор — секретарь Легиона. Точка запуска."""
import logging

import discord
from discord.ext import commands

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("prokurator")

EXTENSIONS = ["cogs.registration", "cogs.warehouses", "cogs.consulate", "cogs.operations", "cogs.edicts", "cogs.elections", "cogs.role_log", "cogs.backup", "cogs.help"]


class Prokurator(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # нужно, чтобы бот видел, кто зашёл на сервер
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Модуль загружен: %s", ext)

        self.tree.on_error = self.on_app_command_error

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Слэш-команд зарегистрировано: %d", len(synced))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        log.exception("Ошибка в команде /%s", interaction.command.name if interaction.command else "?", exc_info=error)
        text = f"⚠️ Ошибка: {error.__class__.__name__}: {getattr(error, 'original', error)}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        log.info("Прокуратор на посту как %s (id %s)", self.user, self.user.id)


def main() -> None:
    config.check()
    config.DATA_DIR.mkdir(exist_ok=True)
    Prokurator().run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
