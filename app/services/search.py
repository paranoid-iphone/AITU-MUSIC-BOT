from sqlalchemy import func


def contains(column, needle: str):
    """Case-insensitive literal substring match (`!` escapes the LIKE wildcards)."""
    escaped = needle.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return func.lower(column).like(f"%{escaped}%", escape="!")
