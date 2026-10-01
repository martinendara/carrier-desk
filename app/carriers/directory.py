"""Contacts on file, seeded for the demo.

This is the **only** source of an OTP destination. A caller can say any phone
number or address they like; the code goes to what is written here, or nowhere.
That is the difference between verifying a carrier and verifying whoever is on
the other end of the line.

An MC that is absent from this table has no contact on file: the call is routed
to onboarding instead of being sent a code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CarrierContact:
    """What we know about a carrier before the call starts."""

    mc_number: str
    legal_name: str
    email: str
    phone: str | None = None


#: Seeded demo carriers. MC numbers are digits only, matching the validation in
#: app.fmcsa.client. Keep at least one MC out of this table so the
#: `routed_onboarding` path is reachable in a demo.
_CONTACTS: dict[str, CarrierContact] = {
    contact.mc_number: contact
    for contact in (
        CarrierContact(
            mc_number="123456",
            legal_name="Ridgeline Freight LLC",
            email="dispatch@ridgelinefreight.com",
            phone="+1-404-555-0142",
        ),
        CarrierContact(
            mc_number="778899",
            legal_name="Two Rivers Trucking Inc",
            email="ops@tworiverstrucking.com",
            phone="+1-307-555-0188",
        ),
        CarrierContact(
            mc_number="445566",
            legal_name="Halstead Carriers Ltd",
            email="m.halstead@gmail.com",
        ),
    )
}


def lookup(mc_number: str) -> CarrierContact | None:
    """Return the contact on file, or None if this MC is not registered.

    Lookup is exact. A fuzzy match here would let a caller reach a contact that
    is not theirs by naming something similar.
    """
    return _CONTACTS.get(str(mc_number).strip())


def all_contacts() -> tuple[CarrierContact, ...]:
    """Every seeded contact. Used by demos and by the directory tests."""
    return tuple(_CONTACTS.values())
