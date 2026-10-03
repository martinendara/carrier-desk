"""The two things the agent may never say, and the two it may not say first.

Both rules here were broken by real calls, not by a hypothetical:

* the close of a failed negotiation said "I can't go higher than that", which
  tells the carrier there is a number we are refusing to reach — the one fact
  the whole design hides — and then promised a rep would follow up, which no
  rep does after a walk-away;
* the pitch read out weight, miles and dimensions to someone driving.

The prompt is part of the contract too, so these read `docs/prompt-live.md`
and `docs/transcription-context.md` as well as the code. A prompt edit that
quietly puts the numbers back should fail here, not on a carrier call.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api import phrasing

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "docs" / "prompt-live.md"
TRANSCRIPTION = REPO_ROOT / "docs" / "transcription-context.md"

#: Words that tell a carrier a ceiling exists. The prompt forbids the agent
#: from saying them; the server's own copy has to hold to the same list.
CAP_WORDS = re.compile(
    r"\b(max|maximum|max\b|limit|budget|ceiling|higher|highest|the most we can pay|cap)\b",
    re.IGNORECASE,
)

#: Promising a human. Fine on most paths — a TMS fault or a locked OTP really
#: does get a callback. Not fine after a negotiation_failed, where the call is
#: over and nobody follows up.
FOLLOW_UP = re.compile(
    r"\b(someone|a rep|a colleague|follow up|call you back|reach out)\b", re.IGNORECASE
)

#: Every agent-facing string, by name.
SAYS = {
    name: value
    for name, value in vars(phrasing).items()
    if name.isupper() and isinstance(value, str) and not name.startswith("_")
}

#: The strings reachable on the failed-negotiation path, in the order a call
#: can hit them: the close itself, then a counter that arrives after the close
#: has already ended the negotiation, then a booking with nothing agreed.
#:
#: Deliberately narrow. `NEGOTIATION_LIMIT` promises "someone to follow up with
#: other options" and does *not* belong here: a close ends the call, so a third
#: negotiation can never follow one — the limit is reached by declining loads,
#: not by failing to agree on them.
FAILED_NEGOTIATION_PATH = ("OFFER_CLOSE", "NEGOTIATION_CLOSED", "NO_NEGOTIATION")


# --- a close must not hint at a ceiling ---------------------------------


def test_the_close_says_what_it_is_without_naming_a_limit():
    assert phrasing.OFFER_CLOSE == (
        "Looks like we're too far apart on this one. "
        "Thanks for your time, and call us back anytime."
    )


def test_the_close_does_not_promise_a_human():
    """Nobody follows up on a walk-away, so the line must not say one will."""
    assert not FOLLOW_UP.search(phrasing.OFFER_CLOSE)


@pytest.mark.parametrize("name", sorted(SAYS))
def test_no_agent_facing_line_hints_at_a_ceiling(name):
    """Every line the agent can speak, not just the one that was wrong.

    "I can't go higher" is the failure this pins: it does not name the ceiling,
    it just confirms one exists, which is enough for a carrier to keep pushing.
    """
    text = SAYS[name]

    match = CAP_WORDS.search(text)
    assert match is None, (
        f"{name} says {match.group(0)!r}: {text!r}. A carrier hearing this learns "
        "there is a number above the one they were offered."
    )


@pytest.mark.parametrize("name", FAILED_NEGOTIATION_PATH)
def test_nothing_on_the_failed_negotiation_path_offers_a_human(name):
    match = FOLLOW_UP.search(SAYS[name])
    assert match is None, (
        f"{name} promises {match.group(0)!r} after a failed negotiation: {SAYS[name]!r}"
    )


def test_the_failed_negotiation_path_is_still_covered_by_this_file():
    """If a say is renamed, the list above must not silently stop testing it."""
    for name in FAILED_NEGOTIATION_PATH:
        assert name in SAYS, f"{name} is gone; update FAILED_NEGOTIATION_PATH"


# --- the prompt: the pitch, and the ending ------------------------------


def _prompt() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _pitch_line(prompt: str) -> str:
    return next(line for line in prompt.splitlines() if line.startswith("Pitch the load"))


def test_the_pitch_leads_with_what_a_driver_needs():
    line = _pitch_line(_prompt()).lower()

    for wanted in ("origin", "destination", "pickup day and time", "equipment"):
        assert wanted in line, f"the pitch no longer mentions {wanted!r}"


def test_the_pitch_does_not_read_out_the_numbers_first():
    """Weight, miles and dimensions are for the carrier to ask for, not to hear.

    They are in the tool result either way — that is checked separately — so
    the only thing being decided here is whether they are read out unprompted.
    """
    line = _pitch_line(_prompt()).lower()

    for deferred in ("weight", "miles", "dimensions", "commodity", "pieces"):
        assert deferred in line, f"{deferred!r} should be named as ask-first"
    assert "unless the carrier asks" in line

    # Named, yes — but not in the list of things to say. The sentence that
    # enumerates the pitch ends before they appear.
    says = line.split("that is all a driver needs")[0]
    for deferred in ("weight", "miles", "dimensions", "commodity", "pieces"):
        assert deferred not in says, f"{deferred!r} is still in the opening pitch"


def test_the_load_response_still_carries_what_the_pitch_defers():
    """The agent can answer a follow-up only if the fields are still there."""
    from test_api import api_app, become_verified, search

    with api_app() as (client, _):
        become_verified(client, "run-1")
        load = search(client, "run-1", equipment="DRY_VAN")["loads"][0]

    for field in ("weight", "miles", "dims", "commodity", "pieces"):
        assert field in load, f"{field} left the response; a follow-up cannot be answered"
        assert load[field] not in (None, ""), f"{field} is present but empty"


def test_a_close_still_hangs_up_and_does_not_transfer():
    prompt = _prompt()
    line = next(l for l in prompt.splitlines() if l.startswith("action close:"))

    assert "_hangup" in line
    assert "in that same turn" in line
    assert "Do not transfer" in line
    for excluded in ("book_load", "handoff with"):
        assert excluded not in line


def test_the_ending_rule_treats_a_defined_ending_as_terminal():
    """The close say does not sound like a goodbye, which is why this exists."""
    ending = _prompt().split("Ending", 1)[1]

    assert "defined ending" in ending
    assert "even when the say does not sound like a goodbye" in ending
    assert "Never say goodbye twice" in ending


def test_the_person_request_cannot_fire_after_the_call_ends():
    prompt = _prompt()
    line = next(l for l in prompt.splitlines() if l.startswith("If they ask for a person"))

    assert "before the call has ended" in line


# --- the prompt: the MC length ------------------------------------------


def test_the_prompt_never_implies_a_fixed_mc_length():
    prompt = _prompt()

    assert "MC numbers are not a fixed length" in prompt
    assert "107012" not in prompt, "a six-digit example is what taught the agent 'six-digit'"


def test_the_prompt_still_asks_for_the_read_back():
    """Dropping the length must not drop the confirmation."""
    prompt = _prompt()

    assert "Read it back digit by digit" in prompt


def test_the_paste_ready_transcription_context_has_no_fixed_mc_length():
    """The document quotes the wrong text on purpose, to explain what changed.

    What gets pasted is the fenced block, so that is what has to be clean —
    asserting on the whole file would fail on the quotation and teach the next
    reader to delete the explanation instead of keeping the block right.
    """
    block = TRANSCRIPTION.read_text(encoding="utf-8").split("## The replacement", 1)[1]
    pasted = block.split("```", 2)[1]

    assert "no fixed length" in pasted
    assert "6 to 7 digits" not in pasted
