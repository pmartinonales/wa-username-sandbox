"""Seed the demo tenant (§11). Run: python -m app.seed
Idempotent: skips if the demo tenant already exists. Prints all keys/ids."""
import asyncio
import json

from sqlalchemy import select

from app import ids, rules
from app.db import Base, SessionLocal, engine
from app.models import (
    BehaviorConfig, BusinessNumber, Consumer, ContactBookEntry, ParentAccount,
    ParentEnrollment, Portfolio, Template, Tenant,
)

DEMO_NAME = "demo-tenant"


async def seed() -> dict:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        existing = (await session.execute(select(Tenant).where(
            Tenant.name == DEMO_NAME))).scalar_one_or_none()
        if existing:
            return {"status": "already seeded", "tenant_id": existing.id,
                    "sandbox_api_key": existing.sandbox_api_key}

        tenant = Tenant(id=await ids.new_id(session, "tn"), name=DEMO_NAME,
                        sandbox_api_key=ids.make_api_key(
                            "sandbox", await ids.next_seq(session, "sandbox_key")))
        session.add(tenant)
        await session.flush()

        pf1 = Portfolio(id=await ids.new_id(session, "pf"), tenant_id=tenant.id,
                        name="Demo Portfolio A", parent_bsuid_enabled=True)
        pf2 = Portfolio(id=await ids.new_id(session, "pf"), tenant_id=tenant.id,
                        name="Demo Portfolio B")
        session.add_all([pf1, pf2])
        await session.flush()

        account = ParentAccount(id=await ids.new_id(session, "pa"), tenant_id=tenant.id)
        session.add(account)
        await session.flush()
        session.add(ParentEnrollment(id=await ids.new_id(session, "pe"),
                                     parent_account_id=account.id, portfolio_id=pf1.id))

        numbers = []
        for portfolio, label in ((pf1, "Acme Shop"), (pf2, "Acme Support")):
            bn = BusinessNumber(
                id=await ids.new_id(session, "bn"), portfolio_id=portfolio.id, name=label,
                display_phone_number="49" + ids.digits(10, "seedbiz", label),
                d360_api_key=ids.make_api_key("d360", await ids.next_seq(session, "d360_key")))
            session.add(bn)
            await session.flush()
            session.add(BehaviorConfig(business_number_id=bn.id, config={}))
            numbers.append(bn)

        alice = Consumer(id=await ids.new_id(session, "cs"), tenant_id=tenant.id,
                         phone="5511999990001", display_name="Alice Username",
                         username="alice.demo", country="BR")
        bob = Consumer(id=await ids.new_id(session, "cs"), tenant_id=tenant.id,
                       phone="5511999990002", display_name="Bob Nousername",
                       username=None, country="BR")
        carol = Consumer(id=await ids.new_id(session, "cs"), tenant_id=tenant.id,
                         phone="5511999990003", display_name="Carol Contactbook",
                         username="carol.demo", country="BR")
        session.add_all([alice, bob, carol])
        await session.flush()
        # carol has an existing contact-book entry with portfolio A
        session.add(ContactBookEntry(id=await ids.new_id(session, "cb"),
                                     portfolio_id=pf1.id, consumer_id=carol.id,
                                     created_at=rules.now_for(tenant), phone_known=True))

        rci_button = {"type": "BUTTONS", "buttons": [
            {"type": "REQUEST_CONTACT_INFO", "text": "Share Contact Info"}]}
        session.add(Template(id=await ids.new_id(session, "tpl"), portfolio_id=pf1.id,
                             name="request_contact_utility", language="en",
                             category="utility",
                             components=[{"type": "BODY", "text": "Please share your contact."},
                                         rci_button]))
        session.add(Template(id=await ids.new_id(session, "tpl"), portfolio_id=pf1.id,
                             name="request_contact_marketing", language="en",
                             category="marketing",
                             components=[{"type": "BODY", "text": "Deals! Share your contact."},
                                         rci_button]))
        await session.commit()

        return {
            "status": "seeded",
            "tenant_id": tenant.id,
            "sandbox_api_key": tenant.sandbox_api_key,
            "portfolios": [{"id": pf1.id, "name": pf1.name, "parent_account_id": account.id},
                           {"id": pf2.id, "name": pf2.name}],
            "business_numbers": [{"id": bn.id, "portfolio_id": bn.portfolio_id,
                                  "display_phone_number": bn.display_phone_number,
                                  "d360_api_key": bn.d360_api_key} for bn in numbers],
            "consumers": [{"id": c.id, "phone": c.phone, "username": c.username}
                          for c in (alice, bob, carol)],
            "templates": ["request_contact_utility", "request_contact_marketing"],
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(seed()), indent=2))
