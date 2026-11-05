import logging

from .bot import MusicBot
from .config import Config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    MusicBot(config).run(config.discord.token, log_handler=None)


if __name__ == "__main__":
    main()
