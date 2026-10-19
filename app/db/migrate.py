from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def alembic_config(database_url: str | None = None) -> Config:
    """Alembic config with absolute paths, so it works from any working directory."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["configure_logging"] = False
    if database_url:
        config.attributes["database_url"] = database_url
    return config


def upgrade_to_head(database_url: str | None = None) -> None:
    """Blocking: env.py runs its own event loop, so call this from async code through asyncio.to_thread."""
    command.upgrade(alembic_config(database_url), "head")
