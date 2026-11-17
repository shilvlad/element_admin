from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Integer, Boolean, Text, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase): pass

def utcnow(): return datetime.now(timezone.utc)

class RegistrationRequest(Base):
    __tablename__ = "registration_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    requester: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(255), index=True)
    displayname: Mapped[str] = mapped_column(String(255), default="")
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

class RegistrationReservation(Base):
    """Serialize registration of each local username, including legacy requests."""
    __tablename__ = "registration_reservations"
    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[int] = mapped_column(Integer, unique=True)

class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[int] = mapped_column(Integer, index=True)

class RegistrationOperation(Base):
    __tablename__ = "registration_operations"
    request_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[int] = mapped_column(Integer)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str] = mapped_column(String(64), default="-")
    details: Mapped[str] = mapped_column(Text, default="")

class ManagedUser(Base):
    __tablename__ = "managed_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255))
    displayname: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class PanelSetting(Base):
    __tablename__ = "panel_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

class NotificationRoom(Base):
    __tablename__ = "notification_rooms"
    sender: Mapped[str] = mapped_column(String(255), primary_key=True)
    recipient: Mapped[str] = mapped_column(String(255), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(255))

def init_db(): Base.metadata.create_all(bind=engine)

def audit(db, actor, action, target, ip="-", details=""):
    db.add(AuditLog(actor=actor, action=action, target=target, ip=ip, details=details))
