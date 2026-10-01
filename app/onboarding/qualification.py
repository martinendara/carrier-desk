"""Lead qualification: a pure function, no LLM.

A language model would give a different answer on Tuesday. This decides whether
a carrier we cannot sell to today is worth a human's time, and it has to give
the same answer for the same inputs every time, so it is a rule.

The thresholds are the ones in CLAUDE.md §8.

`authority_age_days` is **None for every carrier today**: the recorded FMCSA
carrier payload carries no authority date, and the endpoint that might hold one
has not been wired yet (`PROGRESS.md`). Every lead therefore takes the first
rule and lands on `Review`. That is the rule working, not the rule failing —
unknown is treated as unqualified rather than as old enough — but it does mean
qualification is currently a constant until that date is sourced.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Below this, an authority is too new to hand work to.
MIN_AUTHORITY_AGE_DAYS = 90

#: At or above this many matching loads, the carrier is worth calling back today.
HOT_MATCH_THRESHOLD = 3


class LeadTemperature(str, Enum):
    """How the lead is routed on the way out of the call."""

    #: Plenty of freight matched and a settled authority: worth a callback now.
    HOT = "hot"
    #: A settled authority but thin matching freight.
    WARM = "warm"
    #: Needs a human to look: new or unknown authority, or nothing matched.
    REVIEW = "review"


@dataclass(frozen=True)
class LeadQualification:
    """The verdict, plus why, so the event log can explain it later."""

    temperature: LeadTemperature
    reason: str
    authority_age_days: int | None
    matching_loads: int


def qualify(*, authority_age_days: int | None, matching_loads: int) -> LeadQualification:
    """Classify a carrier, evaluating the conditions in the documented order.

    `Review` short-circuits: an authority we cannot date, one younger than 90
    days, or a call where nothing matched all mean a human decides. Only then do
    the matching-load thresholds apply.

    `authority_age_days=None` means the FMCSA payload carried no date we
    recognised. Unknown is treated as unqualified rather than as old enough:
    guessing "probably fine" is how a brand-new authority gets handed a load.
    """
    matches = max(0, int(matching_loads))

    if authority_age_days is None:
        return LeadQualification(
            LeadTemperature.REVIEW, "authority_age_unknown", None, matches
        )

    if authority_age_days < MIN_AUTHORITY_AGE_DAYS:
        return LeadQualification(
            LeadTemperature.REVIEW, "authority_too_new", authority_age_days, matches
        )

    if matches == 0:
        return LeadQualification(
            LeadTemperature.REVIEW, "no_matching_loads", authority_age_days, matches
        )

    if matches >= HOT_MATCH_THRESHOLD:
        return LeadQualification(
            LeadTemperature.HOT, "strong_match", authority_age_days, matches
        )

    return LeadQualification(
        LeadTemperature.WARM, "thin_match", authority_age_days, matches
    )
