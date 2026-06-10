from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Sequence(Base):
    __tablename__ = "sequences"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class ApiKey(Base):
    """One API key = one virtual business phone number with its own implicit
    portfolio (and exactly one simulated consumer counterpart, see UserState)."""
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="sandbox")
    d360_api_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    display_phone_number: Mapped[str] = mapped_column(String)
    waba_id: Mapped[str] = mapped_column(String)
    phone_number_id: Mapped[str] = mapped_column(String)
    portfolio_id: Mapped[str] = mapped_column(String)  # implicit portfolio
    webhook_url: Mapped[str | None] = mapped_column(String, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    # claimed business username
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    username_status: Mapped[str | None] = mapped_column(String, nullable=True)
    # sparse config: only keys the developer explicitly set (deep-merged over defaults)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped["UserState"] = relationship(uselist=False, lazy="selectin")


class UserState(Base):
    """The single simulated consumer behind a key. Config (§3.3 'user') decides
    how they behave; this row holds the state that must persist between calls."""
    __tablename__ = "user_states"
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), primary_key=True)
    phone: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    country: Mapped[str] = mapped_column(String, default="BR")
    bsuid: Mapped[str] = mapped_column(String, index=True)
    parent_bsuid: Mapped[str] = mapped_column(String)
    # real (non-forced) contact book / 30-day cache / window state
    contact_book: Mapped[bool] = mapped_column(Boolean, default=False)
    contact_book_phone_known: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cache_phone_known: Mapped[bool] = mapped_column(Boolean, default=False)
    window_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # whether any phone-addressed traffic happened yet (drives phone-change semantics)
    had_phone_traffic: Mapped[bool] = mapped_column(Boolean, default=False)


class Bsuid(Base):
    """Every BSUID ever attached to a key's user. 'active' values resolve to the
    user; 'retired' ones (pre-phone-change) return 131009. origin='generated'
    rows are key-scoped — using one with another key → 131009 (cross-portfolio);
    origin='supplied' (tester-invented) values may be adopted by many keys,
    since BSUIDs are portfolio-scoped and docs examples are shared."""
    __tablename__ = "bsuids"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), index=True)
    value: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="active")  # active | retired
    origin: Mapped[str] = mapped_column(String, default="generated")  # generated | supplied


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    language: Mapped[str] = mapped_column(String, default="en")
    category: Mapped[str] = mapped_column(String)  # utility | marketing
    components: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="approved")
    auth_flavor: Mapped[str | None] = mapped_column(String, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    wamid: Mapped[str] = mapped_column(String, unique=True, index=True)
    direction: Mapped[str] = mapped_column(String)  # outbound | inbound
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), index=True)
    addressed_by: Mapped[str] = mapped_column(String, default="phone")  # phone | bsuid
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    statuses: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), index=True)
    endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    field: Mapped[str] = mapped_column(String, default="messages")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")  # delivered|failed|logged
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class RequestLog(Base):
    """Every API request/response, for the monitoring UI. Populated by
    middleware; exposed via the schema-hidden GET /sandbox/requests."""
    __tablename__ = "request_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    api_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    method: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    status_code: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_body: Mapped[str | None] = mapped_column(String, nullable=True)
    response_body: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class UsernameChange(Base):
    __tablename__ = "username_changes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), index=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime)
