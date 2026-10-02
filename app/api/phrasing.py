"""Everything the agent might say, in one file.

The agent relays; it does not compose. Keeping the copy here means it can be
read in one sitting, checked for tone, and checked for the thing that matters
most: none of it describes anything internal. No error codes, no host names, no
"the TMS returned a partial response", nothing a carrier could use to work out
how the system is built or where it is weak.

These are written to be spoken, so they are short, and they never ask the
carrier to do something they cannot do.
"""

from __future__ import annotations

# --- errors -------------------------------------------------------------

UNAUTHORIZED = "I can't reach my tools right now. Let me get someone to help you."
NOT_VERIFIED = "Before I can look at loads, I need to confirm the code I sent you."
SESSION_NOT_FOUND = "I've lost the thread of this call. Let me get a colleague to help."
LOAD_NOT_SHOWN = "I can only talk about the loads I've already shown you."
NEGOTIATION_LIMIT = (
    "Let's stick with the loads I've already shown you. "
    "I'll have someone follow up with other options."
)
NO_NEGOTIATION = "I don't have an offer open on that load yet."
NEGOTIATION_CLOSED = "That offer is off the table now."
LOAD_UNAVAILABLE = "I can't get pricing on that one right now. Let me have someone call you."
TMS_TIMEOUT = "Our load board is slow to answer. Let me have someone call you back."
TMS_UNAVAILABLE = "I'm having trouble reaching our load board. Let me get someone to call you."
INVALID_REQUEST = "Sorry, I didn't catch that. Could you say it again?"
INTERNAL = "Something went wrong on my end. Let me get someone to help you."
MC_INVALID = "That MC number doesn't look right. Could you read it to me again?"

# --- carrier verification ----------------------------------------------

CARRIER_ELIGIBLE = "Thanks, I've found your carrier record."
CARRIER_NOT_FOUND = "I couldn't find that MC number in the FMCSA records."
CARRIER_NOT_ALLOWED = "That authority isn't allowed to operate right now."
CARRIER_INACTIVE = "That authority isn't active right now."
CARRIER_OUT_OF_SERVICE = "That carrier is showing as out of service right now."
CARRIER_INSURANCE = (
    "The insurance we need isn't showing as filed on your record. "
    "Let me have someone look into it."
)
CARRIER_UNVERIFIED = "I can't verify your authority right now. Let me have someone call you back."

# --- OTP ----------------------------------------------------------------

OTP_SENT = "I just emailed you a six digit code. It went to the address we have on file, {spoken}."
OTP_SENT_REVIEWER = "I've sent a six digit code to the contact we have on file. Please read me the six digits."
OTP_NOT_VERIFIED = "I need to confirm your carrier first before I can send a code."
OTP_SEND_FAILED = "I couldn't send the code right now. Let me get a colleague to help."
OTP_ROUTED_ONBOARDING = (
    "I don't have a contact email on file for that MC. "
    "Let me take your details and have a rep call you back."
)
OTP_RESEND_LIMIT = "I've already sent that again. Let me get a rep to help you."
OTP_ALREADY_VERIFIED = "You're already verified on this call."
OTP_LOCKED = "That code has been entered too many times. Let me get a rep to help you."
OTP_VERIFIED = "Thanks, that's confirmed."
OTP_RATE_LIMITED = (
    "That's too many code attempts on this carrier for now. Let me get a rep to help you."
)
OTP_WRONG = "That code doesn't match. You have {attempts_left} tries left."
OTP_EXPIRED = "That code has expired. I'll send you a new one."

# --- loads --------------------------------------------------------------

NO_MATCH = "I don't have anything matching that right now. Let me take your details."
LOADS_FOUND = "I've got {count} load{s} that could work."

# --- negotiation --------------------------------------------------------

#: Numbers to words, so a rate is read the way a dispatcher says it and the
#: ear cannot mishear "ten fifty" as "two fifty".
_ONES = [
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty",
    "ninety",
]


def _spell_under_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + ("-" + _ONES[ones] if ones else "")


def _spell_full(n: int) -> str:
    """The whole number in words, e.g. 1050 -> 'one thousand fifty'."""
    thousands, rem = divmod(n, 1000)
    hundreds, rest = divmod(rem, 100)
    parts = []
    if thousands:
        parts.append(f"{_ONES[thousands]} thousand")
    if hundreds:
        parts.append(f"{_ONES[hundreds]} hundred")
    if rest:
        parts.append(_spell_under_100(rest))
    return " ".join(parts)


def spoken_amount(n: int) -> str:
    """A rate the way a dispatcher says it, with the full number for the ear.

    ``1050`` -> ``ten fifty, that's one thousand fifty dollars``;
    ``1500`` -> ``fifteen hundred dollars``;
    ``975``  -> ``nine seventy-five dollars``.
    """
    n = int(n)
    if n <= 0:
        return "zero dollars"
    hundreds, remainder = divmod(n, 100)
    if remainder == 0:
        return f"{_spell_under_100(hundreds)} hundred dollars"
    if hundreds == 0:
        return f"{_spell_under_100(remainder)} dollars"
    trucking = f"{_spell_under_100(hundreds)} {_spell_under_100(remainder)}"
    if n >= 1000:
        return f"{trucking}, that's {_spell_full(n)} dollars"
    return f"{trucking} dollars"


OFFER_OPEN = "I can offer {amount} on that load."
OFFER_ACCEPT = "Great, we're agreed at {amount}."
OFFER_COUNTER = "I can go up to {amount}."
OFFER_CLOSE = "I can't go higher than that on this one. Let me have someone follow up."
ASK_CLARIFY = "Sorry, I want to make sure I heard you right. What rate are you asking for?"

# --- booking and handoff ------------------------------------------------

BOOKED = "You're all set, that load is booked. I'm connecting you with a rep now to wrap up the paperwork."
BOOKING_PENDING = "A rep will confirm your booking in a few minutes. I'm connecting you with them now."
BOOKING_FAILED = "I couldn't complete that booking. Let me have someone call you back."
HANDOFF = "I've asked a rep to pick this up. They'll be with you shortly."
