from alembic import command
from sqlalchemy import create_engine

from app.db.migrate import alembic_config, upgrade_to_head


def _urls(tmp_path):
    path = tmp_path / "roles.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_seeds_default_roles(tmp_path):
    from app.services.permission_catalog import ADMIN_ROLE_NAME, DEFAULT_ROLES, PERMISSIONS

    async_url, sync_url = _urls(tmp_path)
    upgrade_to_head(async_url)

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            roles = {
                name: bool(is_system)
                for name, is_system in conn.exec_driver_sql("SELECT name, is_system FROM roles").all()
            }
            assert roles == {ADMIN_ROLE_NAME: True, "ДСВР": False, "Админ группы": False}
            rows = conn.exec_driver_sql(
                "SELECT r.name, p.permission FROM role_permissions p JOIN roles r ON r.id = p.role_id"
            ).all()
    finally:
        engine.dispose()

    granted: dict[str, set[str]] = {}
    for name, permission in rows:
        granted.setdefault(name, set()).add(permission)
    assert ADMIN_ROLE_NAME not in granted  # the system role holds the whole catalog implicitly
    assert granted == {name: set(permissions) for name, permissions in DEFAULT_ROLES.items()}
    assert set().union(*granted.values()) <= set(PERMISSIONS)


def test_upgrade_moves_legacy_staff_to_assignments(tmp_path):
    async_url, sync_url = _urls(tmp_path)
    config = alembic_config(async_url)
    command.upgrade(config, "0003_schedule_change_booking_date")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO groups (slug, title) VALUES ('music', 'Мьюзик')")
            music_id = conn.exec_driver_sql("SELECT id FROM groups WHERE slug = 'music'").scalar_one()
            for telegram_id, role in ((1, "admin"), (2, "dsvr"), (3, "user"), (4, "user")):
                conn.exec_driver_sql(
                    "INSERT INTO users (telegram_id, language, role, status, notifications_enabled, created_at, updated_at) "
                    f"VALUES ({telegram_id}, 'ru', '{role}', 'approved', 1, '2026-10-01 00:00:00', '2026-10-01 00:00:00')"
                )
            group_admin_id = conn.exec_driver_sql("SELECT id FROM users WHERE telegram_id = 4").scalar_one()
            conn.exec_driver_sql(f"INSERT INTO group_admins (group_id, user_id) VALUES ({music_id}, {group_admin_id})")

        command.upgrade(config, "head")

        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT u.telegram_id, r.name, a.group_id FROM role_assignments a "
                "JOIN users u ON u.id = a.user_id JOIN roles r ON r.id = a.role_id ORDER BY u.telegram_id"
            ).all()
    finally:
        engine.dispose()

    assert rows == [(1, "Админ", None), (2, "ДСВР", None), (4, "Админ группы", music_id)]


def test_downgrade_removes_the_role_tables(tmp_path):
    from sqlalchemy import inspect

    async_url, sync_url = _urls(tmp_path)
    config = alembic_config(async_url)
    role_tables = {"roles", "role_permissions", "role_assignments"}
    command.upgrade(config, "head")

    engine = create_engine(sync_url)
    try:
        assert role_tables <= set(inspect(engine).get_table_names())
        command.downgrade(config, "0003_schedule_change_booking_date")
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not role_tables & tables
    assert "group_admins" in tables
