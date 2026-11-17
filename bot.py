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

EXTENSIONS = ["cogs.registration"]


class Prokurator(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Модуль загружен: %s", ext)

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Слэш-команд зарегистрировано: %d", len(synced))

    async def on_ready(self) -> None:
        log.info("Прокуратор на посту как %s (id %s)", self.user, self.user.id)


def main() -> None:
    config.check()
    config.DATA_DIR.mkdir(exist_ok=True)
    Prokurator().run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
