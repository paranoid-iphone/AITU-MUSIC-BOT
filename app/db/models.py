from datetime import date, datetime, time
from enum import Enum
from uuid import uuid4

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Time, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.timezone import utcnow


class Base(DeclarativeBase):
    pass


GROUP_SLUGS = (("music", "Мьюзик"), ("dance", "Денс"), ("cheer", "Чирлидерши"))


class UserStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BANNED = "banned"
    DELETED = "deleted"


class BookingStatus(str, Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class EventStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(30), unique=True)
    title: Mapped[str] = mapped_column(String(80))


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role", cascade="all, delete-orphan")
    assignments: Mapped[list["RoleAssignment"]] = relationship(back_populates="role", cascade="all, delete-orphan")


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission", name="uq_role_permission"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    permission: Mapped[str] = mapped_column(String(60))

    role: Mapped[Role] = relationship(back_populates="permissions")


class RoleAssignment(Base):
    """A role held by a user: for the whole club (`group_id` is NULL) or for one group."""

    __tablename__ = "role_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    role: Mapped[Role] = relationship(back_populates="assignments")
    user: Mapped["User"] = relationship()
    group: Mapped[Group | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    actor: Mapped["User | None"] = relationship("User", foreign_keys=[actor_id])


class OutboxEvent(Base):
    """A backend -> bot event, stored in the transaction that caused it and pushed to Redis after the commit."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    study_group: Mapped[str | None] = mapped_column(String(50), nullable=True)
    language: Mapped[str] = mapped_column(String(2), default="ru")
    status: Mapped[str] = mapped_column(String(20), default=UserStatus.PENDING.value)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    moderation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registration_retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    bands_created: Mapped[list["Band"]] = relationship(back_populates="creator", foreign_keys="Band.creator_id")
    memberships: Mapped[list["BandMember"]] = relationship(back_populates="user")

    @property
    def profile_completed(self) -> bool:
        return bool(self.first_name and self.last_name and self.study_group and self.language)


class Band(Base):
    __tablename__ = "bands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), index=True)
    invite_code: Mapped[str] = mapped_column(String(30), unique=True, index=True, default=lambda: uuid4().hex[:8].upper())
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    responsible_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    creator: Mapped[User] = relationship(back_populates="bands_created", foreign_keys=[creator_id])
    group: Mapped[Group | None] = relationship()
    responsible: Mapped[User | None] = relationship(foreign_keys=[responsible_user_id])
    members: Mapped[list["BandMember"]] = relationship(back_populates="band", cascade="all, delete-orphan")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="band")


class BandMember(Base):
    __tablename__ = "band_members"
    __table_args__ = (UniqueConstraint("band_id", "user_id", name="uq_band_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    band_id: Mapped[int] = mapped_column(ForeignKey("bands.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    instrument_role: Mapped[str] = mapped_column(String(80), default="other")
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    band: Mapped[Band] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class AvailableDay(Base):
    __tablename__ = "available_days"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day_of_week: Mapped[int] = mapped_column(Integer, unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    occupied_by: Mapped[str | None] = mapped_column(String(150))


class ClubSetting(Base):
    __tablename__ = "club_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    band_id: Mapped[int | None] = mapped_column(ForeignKey("bands.id"), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    booking_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    purpose: Mapped[str] = mapped_column(String(30), default="self")
    song_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=BookingStatus.CONFIRMED.value)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repeat_offered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    band: Mapped[Band | None] = relationship(back_populates="bookings")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])


class MoveRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class MoveRequest(Base):
    """Someone asks the owners of `booking_id` to move it to a free slot of the same length."""

    __tablename__ = "move_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    proposed_date: Mapped[date] = mapped_column(Date)
    proposed_start: Mapped[time] = mapped_column(Time)
    proposed_end: Mapped[time] = mapped_column(Time)
    message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=MoveRequestStatus.PENDING.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    booking: Mapped[Booking] = relationship()
    requester: Mapped[User] = relationship(foreign_keys=[requester_id])


class WaitlistEntry(Base):
    """`user_id` waits for an occupied range to free up, to book it for `band_id` (null = a personal rehearsal)."""

    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    band_id: Mapped[int | None] = mapped_column(ForeignKey("bands.id"), nullable=True, index=True)
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    band: Mapped[Band | None] = relationship()


class WeekStatus(str, Enum):
    COLLECTING = "collecting"
    PUBLISHED = "published"


class RequestStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"


class SchedulingWeek(Base):
    """One planning week (Monday-based). Requests are collected, then allocated and published once."""

    __tablename__ = "scheduling_weeks"

    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default=WeekStatus.COLLECTING.value)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AvailabilityRequest(Base):
    """A band's request for one rehearsal next week, with up to five time windows ordered by priority."""

    __tablename__ = "availability_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    band_id: Mapped[int] = mapped_column(ForeignKey("bands.id"))
    submitted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(30), default="self")
    song_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.PENDING.value)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    band: Mapped[Band] = relationship()
    submitted_by: Mapped[User] = relationship(foreign_keys=[submitted_by_id])
    windows: Mapped[list["AvailabilityWindow"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="AvailabilityWindow.priority"
    )


class AvailabilityWindow(Base):
    __tablename__ = "availability_windows"
    __table_args__ = (UniqueConstraint("request_id", "priority", name="uq_request_priority"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("availability_requests.id"))
    priority: Mapped[int] = mapped_column(Integer)
    day_of_week: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    request: Mapped[AvailabilityRequest] = relationship(back_populates="windows")


class ScheduleChange(Base):
    """Who changed the schedule and how. Rows outlive bookings and bands, so names are denormalized."""

    __tablename__ = "schedule_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True)
    band_id: Mapped[int | None] = mapped_column(ForeignKey("bands.id", ondelete="SET NULL"), nullable=True, index=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True, index=True)
    band_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    booking_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(30))
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sheets_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    actor: Mapped["User | None"] = relationship("User", foreign_keys=[actor_id])


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(150))
    event_date: Mapped[date] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(150), nullable=True)
    submission_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=EventStatus.OPEN.value)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    applications: Mapped[list["EventApplication"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class EventApplication(Base):
    __tablename__ = "event_applications"
    __table_args__ = (UniqueConstraint("event_id", "band_id", name="uq_event_band_application"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    band_id: Mapped[int] = mapped_column(ForeignKey("bands.id"))
    submitted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    song_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped[Event] = relationship(back_populates="applications")
    band: Mapped[Band] = relationship()
    members: Mapped[list["EventApplicationMember"]] = relationship(back_populates="application", cascade="all, delete-orphan")


class EventApplicationMember(Base):
    __tablename__ = "event_application_members"
    __table_args__ = (UniqueConstraint("application_id", "user_id", name="uq_application_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("event_applications.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    instrument_role: Mapped[str] = mapped_column(String(80))

    application: Mapped[EventApplication] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


class PersonalEventHistory(Base):
    __tablename__ = "personal_event_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(150))
    event_date: Mapped[date] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(150), nullable=True)
    role_at_event: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship()
