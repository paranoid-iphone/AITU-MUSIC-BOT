"""The permissions a role can hold and the roles a fresh club starts with. Constants only: no database access."""

ADMIN_ROLE_NAME = "Админ"

# Permission -> where it can be granted: "club" (only without a group) or "group" (also for a single group).
PERMISSIONS: dict[str, str] = {
    "users.moderate": "group",
    "bands.manage": "group",
    "bookings.manage": "group",
    "bookings.priority": "club",
    "schedule.history": "group",
    "audit.view": "club",
    "settings.manage": "club",
    "events.manage": "club",
    "roles.manage": "club",
}

# What today's DSVR and group admins can do. The admin role is not listed: it holds the whole catalog implicitly.
DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "ДСВР": ("bookings.priority", "bookings.manage", "schedule.history"),
    "Админ группы": ("users.moderate", "bands.manage", "bookings.manage", "schedule.history"),
}
