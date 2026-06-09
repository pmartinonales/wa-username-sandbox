from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Sequence(Base):
    __tablename__ = "sequences"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    sandbox_api_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    ga_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    clock_offset_s: Mapped[int] = mapped_column(Integer, default=0)
    # tenant-level default for status webhook delays (overridable per number)
    status_delays_ms: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Portfolio(Base):
    __tablename__ = "portfolios"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    parent_bsuid_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class BusinessNumber(Base):
    __tablename__ = "business_numbers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"), index=True)
    name: Mapped[str] = mapped_column(String, default="Business")
    display_phone_number: Mapped[str] = mapped_column(String)
    d360_api_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    webhook_url: Mapped[str | None] = mapped_column(String, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    username_status: Mapped[str | None] = mapped_column(String, nullable=True)

    behavior: Mapped["BehaviorConfig"] = relationship(uselist=False, lazy="selectin")


class BehaviorConfig(Base):
    __tablename__ = "behavior_configs"
    business_number_id: Mapped[str] = mapped_column(
        ForeignKey("business_numbers.id"), primary_key=True
    )
    # sparse: only keys the tester explicitly set; effective config merges defaults
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class Consumer(Base):
    __tablename__ = "consumers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    phone: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[str] = mapped_column(String)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str] = mapped_column(String, default="BR")


class BsuidMapping(Base):
    __tablename__ = "bsuid_mappings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("consumers.id"), index=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"), index=True)
    bsuid: Mapped[str] = mapped_column(String, index=True)
    __table_args__ = (UniqueConstraint("consumer_id", "portfolio_id"),)


class ParentAccount(Base):
    __tablename__ = "parent_accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)


class ParentEnrollment(Base):
    __tablename__ = "parent_enrollments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_account_id: Mapped[str] = mapped_column(ForeignKey("parent_accounts.id"), index=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"), index=True, unique=True)


class ParentBsuid(Base):
    __tablename__ = "parent_bsuids"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("consumers.id"), index=True)
    parent_account_id: Mapped[str] = mapped_column(ForeignKey("parent_accounts.id"), index=True)
    value: Mapped[str] = mapped_column(String, index=True)
    __table_args__ = (UniqueConstraint("consumer_id", "parent_account_id"),)


class ContactBookEntry(Base):
    __tablename__ = "contact_book_entries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"), index=True)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("consumers.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # False for entries written by delivered BSUID sends: they exist but do not
    # reveal the phone (otherwise a single BSUID reply would defeat BSUID-only mode)
    phone_known: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("portfolio_id", "consumer_id"),)


class CacheEntry(Base):
    __tablename__ = "cache_entries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    business_number_id: Mapped[str] = mapped_column(ForeignKey("business_numbers.id"), index=True)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("consumers.id"), index=True)
    last_interaction_at: Mapped[datetime] = mapped_column(DateTime)
    phone_known: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("business_number_id", "consumer_id"),)


class ServiceWindow(Base):
    __tablename__ = "service_windows"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    business_number_id: Mapped[str] = mapped_column(ForeignKey("business_numbers.id"), index=True)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("consumers.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint("business_number_id", "consumer_id"),)


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"), index=True)
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
    business_number_id: Mapped[str] = mapped_column(ForeignKey("business_numbers.id"), index=True)
    consumer_id: Mapped[str] = mapped_column(ForeignKey("consumers.id"), index=True)
    addressed_by: Mapped[str] = mapped_column(String, default="phone")  # phone | bsuid
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    statuses: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    business_number_id: Mapped[str] = mapped_column(ForeignKey("business_numbers.id"), index=True)
    endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    field: Mapped[str] = mapped_column(String, default="messages")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")  # delivered|failed|logged
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class UsernameChange(Base):
    __tablename__ = "username_changes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    business_number_id: Mapped[str] = mapped_column(ForeignKey("business_numbers.id"), index=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime)
