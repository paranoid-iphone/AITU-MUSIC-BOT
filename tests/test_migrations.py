from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.db import models
from app.db.migrate import alembic_config, upgrade_to_head


def _urls(tmp_path):
    path = tmp_path / "migrated.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_migrations_match_the_models(tmp_path):
    """Fails when a model changes without a new revision in alembic/versions."""
    async_url, sync_url = _urls(tmp_path)
    upgrade_to_head(async_url)

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), models.Base.metadata)
    finally:
        engine.dispose()
    assert diff == []


def test_upgrade_is_repeatable_and_creates_every_table(tmp_path):
    async_url, sync_url = _urls(tmp_path)
    upgrade_to_head(async_url)
    upgrade_to_head(async_url)  # already at head: a no-op

    engine = create_engine(sync_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert set(models.Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_downgrade_to_base_and_up_again(tmp_path):
    async_url, sync_url = _urls(tmp_path)
    config = alembic_config(async_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(sync_url)
    try:
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
    command.upgrade(config, "head")


def test_upgrade_adopts_a_database_created_by_create_all(tmp_path):
    """A database made by the old startup code (no alembic_version) is adopted without touching its data."""
    async_url, sync_url = _urls(tmp_path)
    engine = create_engine(sync_url)
    try:
        models.Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO groups (slug, title) VALUES ('music', 'Мьюзик')")
    finally:
        engine.dispose()

    upgrade_to_head(async_url)

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT slug FROM groups").scalars().all() == ["music"]
            head = ScriptDirectory.from_config(alembic_config(async_url)).get_current_head()
            assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == head
    finally:
        engine.dispose()


def test_time_slots_are_dropped_with_their_rows(tmp_path):
    async_url, sync_url = _urls(tmp_path)
    config = alembic_config(async_url)
    command.upgrade(config, "0001_baseline")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO time_slots (day_of_week, start_time, end_time, is_enabled) VALUES (0, '18:00:00', '19:00:00', 1)"
            )
        command.upgrade(config, "head")
        assert "time_slots" not in inspect(engine).get_table_names()
        command.downgrade(config, "0001_baseline")
        assert "time_slots" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
