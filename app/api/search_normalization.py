"""Normalise the agent's free-text search filters into canonical TMS values.

A voice agent transcribes a carrier's words, so the filters arrive as spoken:
``dry van``, ``Chicago, Illinois``, ``anywhere``. The TMS matches exact
upper-case identifiers (``DRY_VAN``, ``IL``), so this layer turns the spoken
forms into them before the ``LOAD_QUERY`` is built. It lives here rather than in
the TMS client because the TMS is a separate, legacy system we do not change.
"""

from __future__ import annotations

#: Full state name -> two-letter abbreviation. Keys are lowercased.
STATE_ABBREVIATIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

#: The two-letter codes themselves (lowercased keys), so an already-abbreviated
#: state passes through case-insensitively ("il" -> "IL").
_STATE_CODES = {code.lower(): code for code in STATE_ABBREVIATIONS.values()}

#: Spoken equipment variants -> canonical TMS EQTYPE. Keys are lowercased.
EQUIPMENT_ALIASES = {
    "dry van": "DRY_VAN",
    "van": "DRY_VAN",
    "dryvan": "DRY_VAN",
    "dry_van": "DRY_VAN",
    "dry-van": "DRY_VAN",
    "reefer": "REEFER",
    "refrigerated": "REEFER",
    "refrigerator": "REEFER",
    "flatbed": "FLATBED",
    "flat bed": "FLATBED",
}

#: Words a carrier says that mean "no destination preference".
_ANYWHERE = {"anywhere", "any"}


def _fold(value: str | None) -> str:
    """Lowercase and collapse whitespace, so "Dry  Van" matches "dry van"."""
    return " ".join((value or "").strip().lower().split())


def normalize_equipment(value: str | None) -> str | None:
    """``dry van`` -> ``DRY_VAN``. Unknown words become upper-snake and pass through."""
    text = _fold(value)
    if not text:
        return None
    if text in EQUIPMENT_ALIASES:
        return EQUIPMENT_ALIASES[text]
    # An unrecognised spoken form is still sent, canonicalised, so the TMS can
    # match it if it has that equipment — and finds nothing if it does not.
    return text.replace(" ", "_").replace("-", "_").upper()


def normalize_state(value: str | None) -> str | None:
    """``Illinois`` or ``il`` -> ``IL``. Unrecognised -> None (no filter)."""
    text = _fold(value)
    if not text:
        return None
    if text in _STATE_CODES:
        return _STATE_CODES[text]
    return STATE_ABBREVIATIONS.get(text)


def split_city_state(value: str | None) -> tuple[str | None, str | None]:
    """``Chicago, Illinois`` -> (``Chicago``, ``Illinois``); no comma -> (city, None)."""
    text = (value or "").strip()
    if not text:
        return None, None
    if "," not in text:
        return text, None
    city, _, state = text.partition(",")
    return city.strip() or None, state.strip() or None


def is_anywhere(value: str | None) -> bool:
    """Whether a destination field means "no preference"."""
    return _fold(value) in _ANYWHERE


def build_search_filters(
    *,
    equipment: str | None = None,
    orig_city: str | None = None,
    orig_state: str | None = None,
    orig_zip: str | None = None,
    dest_city: str | None = None,
    dest_state: str | None = None,
    dest_zip: str | None = None,
    pickup_date: str | None = None,
) -> dict[str, str]:
    """Turn the request's free-text fields into canonical TMS filter values."""
    filters: dict[str, str] = {}

    eqtype = normalize_equipment(equipment)
    if eqtype:
        filters["EQTYPE"] = eqtype

    # Origin. The city field may carry a state ("Chicago, Illinois").
    city, state_in_city = split_city_state(orig_city)
    state = normalize_state(orig_state or state_in_city)
    if city:
        filters["ORIG_CITY"] = city
    if state:
        filters["ORIG_STATE"] = state
    if orig_zip and orig_zip.strip():
        filters["ORIG_ZIP"] = orig_zip.strip()

    # Destination. Empty or "anywhere" means no destination filter at all.
    dcity, dstate_in_city = split_city_state(dest_city)
    dstate = normalize_state(dest_state or dstate_in_city)
    if dcity and not is_anywhere(dcity):
        filters["DEST_CITY"] = dcity
    if dstate and not is_anywhere(dest_state or dstate_in_city):
        filters["DEST_STATE"] = dstate
    if dest_zip and dest_zip.strip() and not is_anywhere(dest_zip):
        filters["DEST_ZIP"] = dest_zip.strip()

    if pickup_date and pickup_date.strip():
        filters["PICKUP_DT"] = pickup_date.strip()

    return filters
