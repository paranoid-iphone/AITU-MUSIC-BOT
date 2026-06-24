from datetime import date, datetime, time
from enum import Enum
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Time, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"


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


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    actor: Mapped["User | None"] = relationship("User", foreign_keys=[actor_id])


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
    role: Mapped[str] = mapped_column(String(20), default=UserRole.USER.value)
    status: Mapped[str] = mapped_column(String(20), default=UserStatus.PENDING.value)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    moderation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registration_retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bands_created: Mapped[list["Band"]] = relationship(back_populates="creator")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    creator: Mapped[User] = relationship(back_populates="bands_created")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    band: Mapped[Band] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class AvailableDay(Base):
    __tablename__ = "available_days"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day_of_week: Mapped[int] = mapped_column(Integer, unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class TimeSlot(Base):
    __tablename__ = "time_slots"
    __table_args__ = (UniqueConstraint("day_of_week", "start_time", "end_time", name="uq_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day_of_week: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ClubSetting(Base):
    __tablename__ = "club_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (UniqueConstraint("booking_date", "start_time", "end_time", name="uq_booking_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    band_id: Mapped[int | None] = mapped_column(ForeignKey("bands.id"), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    booking_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    purpose: Mapped[str] = mapped_column(String(30), default="self")
    song_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=BookingStatus.CONFIRMED.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    band: Mapped[Band | None] = relationship(back_populates="bookings")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(150))
    event_date: Mapped[date] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(150), nullable=True)
    submission_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=EventStatus.OPEN.value)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    applications: Mapped[list["EventApplication"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class EventApplication(Base):
    __tablename__ = "event_applications"
    __table_args__ = (UniqueConstraint("event_id", "band_id", name="uq_event_band_application"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    band_id: Mapped[int] = mapped_column(ForeignKey("bands.id"))
    submitted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    song_title: Mapped[str | None] = mapped_column(String(150), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
