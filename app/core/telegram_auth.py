import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# Telegram and this server may disagree on the time by a few seconds; anything further ahead is forged.
FUTURE_SKEW_SECONDS = 60


def validate_init_data(init_data: str | None, bot_token: str, max_age_seconds: int = 24 * 60 * 60) -> dict | None:
    """Validate Telegram Mini App initData and return the Telegram user dict, or None if invalid.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    age = time.time() - auth_date
    if age > max_age_seconds or age < -FUTURE_SKEW_SECONDS:
        return None
    try:
        user = json.loads(pairs.get("user", "null"))
    except json.JSONDecodeError:
        return None
    return user if isinstance(user, dict) and "id" in user else None
