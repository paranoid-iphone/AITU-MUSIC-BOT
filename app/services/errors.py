import re


class AppError(Exception):
    status_code = 400


class UnauthorizedError(AppError):
    status_code = 401


class NotFoundError(AppError):
    status_code = 404


class ForbiddenError(AppError):
    status_code = 403


class ConflictError(AppError):
    status_code = 409


# Stable codes for the messages people can run into in the Mini App. The message stays English (the bot and the logs
# read it); the API adds the code, and the Mini App shows its own translation of it. A message that is not listed
# here simply reaches the client without a code.
_FIXED_MESSAGES = (
    # booking rules
    ("slot_unavailable", "Slot is not available"),
    ("outside_hours", "Booking is outside room working hours"),
    ("bad_duration", "Booking duration must be a positive 30-minute step"),
    ("day_unavailable", "This day is not available"),
    ("start_in_past", "Start time must be in the future"),
    ("booking_window", "Booking must start in the future and within the next 7 days"),
    ("date_window", "Booking date must be within the next 7 days"),
    ("past_booking", "Past bookings cannot be changed"),
    ("cancelled_booking", "Cancelled booking cannot be changed"),
    ("already_cancelled", "Booking is already cancelled"),
    ("already_started", "This booking has already started"),
    ("not_movable", "This booking cannot be moved"),
    ("not_repeatable", "This booking cannot be repeated"),
    ("purpose_mismatch", "Personal booking must use personal purpose"),
    ("purpose_mismatch", "Band booking cannot use personal purpose"),
    ("purpose_mismatch", "Band booking must use concert or self purpose"),
    ("purpose_mismatch", "Booking without a band must use personal or staff purpose"),
    ("personal_disabled", "Personal bookings are disabled"),
    ("week_not_published", "The schedule for that week is not published yet: send a weekly request instead"),
    # who may do what
    ("not_band_member", "You are not a member of this band"),
    ("no_band_access", "You do not have access to this band"),
    ("manage_band_only", "Only band owner or group admin can manage this band"),
    ("transfer_owner_first", "Transfer ownership before removing the band owner"),
    ("cannot_manage_booking", "You cannot manage this booking"),
    ("change_own_booking", "You can change your own booking yourself"),
    ("takeover_only", "Only DSVR or admins can take over time"),
    ("cannot_repeat", "You cannot repeat this booking"),
    ("admin_required", "Admin access required"),
    ("user_banned", "User is banned"),
    ("not_approved", "Your registration is not approved"),
    ("not_approved", "User is not approved"),
    # things that were not found
    ("booking_not_found", "Booking not found"),
    ("band_not_found", "Band not found"),
    ("member_not_found", "Member not found"),
    ("user_not_found", "User not found"),
    ("invite_not_found", "Invite code not found"),
    ("entry_not_found", "Waiting-list entry not found"),
    ("request_not_found", "Move request not found"),
    # move requests and the waiting list
    ("request_closed", "This request is already closed"),
    ("request_expired", "This request has expired"),
    ("request_not_yours", "This is not your request"),
    ("request_cannot_answer", "You cannot answer this request"),
    ("request_duplicate", "You have already asked to move this booking"),
    ("proposal_length", "The proposed slot must be as long as the booking"),
    ("proposal_same", "The proposed slot is the current one"),
    ("already_waiting", "You are already waiting for this time"),
    ("time_is_free", "This time is free: book it directly"),
    ("staff_time", "This time is held by the studio staff"),
    # administration
    ("hours_order", "Start time must be earlier than end time"),
    ("hours_grid", "Room hours must start and end on the half hour"),
    ("limit_order", "Daily limit cannot be longer than the weekly limit"),
    ("self_moderation", "You cannot ban, reject or delete yourself"),
    ("initial_admin_protected", "Initial admin cannot be banned, rejected or deleted"),
    ("initial_admin_protected", "Initial admin cannot be demoted"),
    ("own_admin_role", "You cannot remove your own admin role"),
    ("username_ambiguous", "Several users share this username: use the numeric Telegram ID"),
    ("group_not_found", "Group not found"),
    # roles
    ("role_not_found", "Role not found"),
    ("role_name_invalid", "Role name must be 1-60 characters"),
    ("role_name_taken", "A role with this name already exists"),
    ("unknown_permission", "Unknown permission"),
    ("system_role", "System role cannot be changed"),
    ("assignment_not_found", "Assignment not found"),
    ("role_already_assigned", "Role is already assigned"),
    ("role_needs_approval", "User must be approved to receive a role"),
    ("admin_role_club_wide", "The admin role applies to the whole club"),
    ("admin_role_admins_only", "Only an admin can assign or revoke the admin role"),
)

# Messages that carry a number; `n` is passed to the client as a parameter.
_NUMBERED_MESSAGES = (
    ("weekly_limit", re.compile(r"^Weekly booking limit exceeded: (?P<n>[\d.]+)h per band$")),
    ("daily_limit", re.compile(r"^Daily booking limit exceeded: (?P<n>[\d.]+)h per band$")),
    ("future_limit", re.compile(r"^Too many future bookings: at most (?P<n>\d+) per band$")),
    ("horizon", re.compile(r"^Date must be within the next (?P<n>\d+) days$")),
    ("too_many_requests", re.compile(r"^At most (?P<n>\d+) open move requests at a time$")),
    ("too_many_entries", re.compile(r"^At most (?P<n>\d+) waiting-list entries at a time$")),
)

_CODE_BY_MESSAGE = {message: code for code, message in _FIXED_MESSAGES}
ERROR_CODES = frozenset({code for code, _ in _FIXED_MESSAGES} | {code for code, _ in _NUMBERED_MESSAGES})


def describe_error(message: str) -> dict:
    """The JSON body of an error response: the message, plus a `code` (and `params`) when the message is a known one."""
    body: dict = {"detail": message}
    code = _CODE_BY_MESSAGE.get(message)
    if code is not None:
        body["code"] = code
        return body
    for code, pattern in _NUMBERED_MESSAGES:
        match = pattern.match(message)
        if match:
            body["code"] = code
            body["params"] = match.groupdict()
            break
    return body
