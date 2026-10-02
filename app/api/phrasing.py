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

OTP_SENT = "I've sent a six digit code to the email we have on file, {masked}."
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

OFFER_OPEN = "I can offer {amount} dollars on that load."
OFFER_ACCEPT = "Great, we're agreed at {amount} dollars."
OFFER_COUNTER = "I can go up to {amount} dollars."
OFFER_CLOSE = "I can't go higher than that on this one. Let me have someone follow up."

# --- booking and handoff ------------------------------------------------

BOOKED = "You're all set — that load is booked."
BOOKING_PENDING = "I've put a hold on that load. A rep will confirm the details shortly."
BOOKING_FAILED = "I couldn't complete that booking. Let me have someone call you back."
HANDOFF = "I've asked a rep to pick this up. They'll be with you shortly."
