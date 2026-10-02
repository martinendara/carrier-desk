"""The negotiation state machine, on top of the run session.

Everything the agent never sees lives here: the ceiling, the gap, the round
counter. The agent gets `{action, amount}` and nothing else, and it cannot skip
a round because it is not the one counting them.

`policy.py` holds the arithmetic; this file holds the memory and the rules about
what may be negotiated at all.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from fractions import Fraction
from typing import Callable, Sequence

from app.events import EventSink, EventType, NullEventSink
from app.negotiation.errors import (
    CeilingUnavailable,
    NegotiationAlreadyClosed,
    NoActiveNegotiation,
)
from app.negotiation.policy import (
    DEFAULT_CEILING_MARGIN,
    DEFAULT_INCREMENT,
    concession_step,
    is_urgent,
    opening_offer,
)
from app.sessions import (
    LoadNotShown,
    NegotiationLimitReached,
    NegotiationOutcome,
    NegotiationSession,
    RunSession,
    RunSessionStore,
    utcnow,
)
from app.tms.models import LoadDetail

logger = logging.getLogger(__name__)

#: A counter retry that lands inside this window with the same ask is a replay
#: of the answer that round already produced, not a new round. Long enough to
#: cover a webhook retry, short enough that a genuine second counter cannot be
#: mistaken for one.
COUNTER_REPLAY_WINDOW = timedelta(seconds=15)


class NegotiationAction(str, Enum):
    """What the agent should do with the number it is handed."""

    ACCEPT = "accept"
    COUNTER = "counter"
    CLOSE = "close"


@dataclass(frozen=True)
class Decision:
    """The answer to one carrier request."""

    action: NegotiationAction
    amount: int
    round_number: int

    @property
    def agent_view(self) -> dict[str, object]:
        """The whole of what the agent may know. No ceiling, no gap, no steps."""
        return {"action": self.action.value, "amount": self.amount}


@dataclass(frozen=True)
class NegotiationOffer:
    """The opening offer, as handed to the agent."""

    load_id: str
    offer: int
    urgent: bool
    #: Internal handle. Excluded from the repr, and deliberately absent from
    #: `agent_view`: the agent never passes it back, so it never needs it.
    negotiation_id: str = field(repr=False)

    @property
    def agent_view(self) -> dict[str, object]:
        return {"load_id": self.load_id, "offer": self.offer}


class NegotiationEngine:
    """Opens negotiations and answers carrier requests."""

    def __init__(
        self,
        store: RunSessionStore,
        *,
        steps: Sequence[Fraction],
        urgent_steps: Sequence[Fraction],
        max_rounds: int = 3,
        increment: int = DEFAULT_INCREMENT,
        ceiling_margin: int = DEFAULT_CEILING_MARGIN,
        clock: Callable[[], datetime] = utcnow,
        sink: EventSink | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if not steps or not urgent_steps:
            raise ValueError("both concession ladders are required")
        self._store = store
        self._steps = tuple(steps)
        self._urgent_steps = tuple(urgent_steps)
        self._max_rounds = max_rounds
        self._increment = increment
        self._margin = ceiling_margin
        self._clock = clock
        self._sink: EventSink = sink or NullEventSink()
        self._new_id = id_factory or (lambda: f"neg_{secrets.token_hex(8)}")

    @classmethod
    def from_settings(cls, store: RunSessionStore, settings, *, sink: EventSink | None = None):
        return cls(
            store,
            steps=settings.negotiation_steps_tuple,
            urgent_steps=settings.negotiation_urgent_steps_tuple,
            max_rounds=settings.negotiation_max_rounds,
            increment=settings.negotiation_increment,
            ceiling_margin=settings.negotiation_ceiling_margin,
            sink=sink,
        )

    # --- opening --------------------------------------------------------

    def open(self, run_id: str, load: LoadDetail) -> NegotiationOffer:
        """Start negotiating `load`.

        The load must have been shown during this call, and the call must still
        have a negotiation left. Both are checked here rather than trusting the
        caller, because the caller is a language model.
        """
        session = self._store.get_or_create(run_id)

        if not session.was_shown(load.load_id):
            raise LoadNotShown(load.load_id)

        if not session.can_open_negotiation:
            raise NegotiationLimitReached(
                f"run {run_id!r} already used its negotiations for this call"
            )

        ceiling = self._require_ceiling(load)

        now = self._clock()
        urgent = is_urgent(load.pickup_dt, now)
        offer = opening_offer(
            load.rate, ceiling, increment=self._increment, ceiling_margin=self._margin
        )

        negotiation = NegotiationSession(
            negotiation_id=self._new_id(),
            run_id=session.run_id,
            load_id=load.load_id,
            ceiling=ceiling,
            posted_rate=load.rate,
            opening_offer=offer,
            current_offer=offer,
            urgent=urgent,
        )
        session.add_negotiation(negotiation)

        # Events are internal, so the ceiling is allowed here and nowhere else.
        self._sink.emit(
            session.run_id,
            EventType.NEGOTIATION_OPENED,
            negotiation_id=negotiation.negotiation_id,
            load_id=load.load_id,
            posted_rate=load.rate,
            ceiling=ceiling,
            opening_offer=offer,
            urgent=urgent,
        )

        return NegotiationOffer(
            load_id=load.load_id,
            offer=offer,
            urgent=urgent,
            negotiation_id=negotiation.negotiation_id,
        )

    # --- rounds ---------------------------------------------------------

    def counter(self, run_id: str, carrier_ask: int | str) -> Decision:
        """Answer one carrier request. Every call here is exactly one round.

        A retry of the same ask inside :data:`COUNTER_REPLAY_WINDOW` is a replay
        of the answer that round already produced — it does not consume a round.
        """
        session = self._store.require(run_id)
        ask = _validate_ask(carrier_ask)

        # Replay first, before the open/closed check: a retry of the round that
        # just accepted or closed must return the same decision, not "already
        # closed".
        replay = self._replay(session, ask)
        if replay is not None:
            return replay

        negotiation = self._active_or_raise(run_id, session.negotiations)
        negotiation.round_number += 1
        round_number = negotiation.round_number
        negotiation.updated_at = self._clock()

        # 1. At or below what is already on the table: take it. The carrier is
        #    asking for less than we offered, and there is no reason to talk them
        #    up.
        if ask <= negotiation.current_offer:
            return self._remember(
                negotiation, ask, self._accept(session, negotiation, ask, round_number)
            )

        # 2. The concession rounds.
        if round_number < self._max_rounds:
            step = concession_step(
                negotiation.opening_offer,
                negotiation.ceiling,
                negotiation.current_offer,
                round_number,
                urgent=negotiation.urgent,
                steps=self._steps,
                urgent_steps=self._urgent_steps,
                increment=self._increment,
                ceiling_margin=self._margin,
            )
            if ask <= step:
                return self._remember(
                    negotiation, ask, self._accept(session, negotiation, ask, round_number)
                )

            negotiation.current_offer = step
            self._sink.emit(
                session.run_id,
                EventType.NEGOTIATION_ROUND,
                negotiation_id=negotiation.negotiation_id,
                load_id=negotiation.load_id,
                round=round_number,
                action=NegotiationAction.COUNTER.value,
                carrier_ask=ask,
                offer=step,
                ceiling=negotiation.ceiling,
            )
            return self._remember(
                negotiation,
                ask,
                Decision(action=NegotiationAction.COUNTER, amount=step, round_number=round_number),
            )

        # 3. The last round: anything up to the ceiling is a deal, and the
        #    ceiling itself is the line. Past it, the call ends here.
        if ask <= negotiation.ceiling:
            return self._remember(
                negotiation, ask, self._accept(session, negotiation, ask, round_number)
            )

        negotiation.outcome = NegotiationOutcome.CLOSED
        negotiation.gap = ask - negotiation.ceiling
        negotiation.updated_at = self._clock()
        self._sink.emit(
            session.run_id,
            EventType.NEGOTIATION_CLOSED,
            negotiation_id=negotiation.negotiation_id,
            load_id=negotiation.load_id,
            outcome=NegotiationOutcome.CLOSED.value,
            reason="no_deal",
            carrier_ask=ask,
            ceiling=negotiation.ceiling,
            gap=negotiation.gap,
            rounds=round_number,
        )
        # The amount is the last offer, never the ceiling: reporting `C` here
        # would hand the agent the number the whole design hides. The last offer
        # is at least one increment below `C` by construction.
        return self._remember(
            negotiation,
            ask,
            Decision(
                action=NegotiationAction.CLOSE,
                amount=negotiation.current_offer,
                round_number=round_number,
            ),
        )

    def _replay(self, session: RunSession, ask: int) -> Decision | None:
        """The earlier decision for `ask`, if it is recent enough to be a retry."""
        if not session.negotiations:
            return None
        negotiation = session.negotiations[-1]
        if negotiation.last_counter_ask != ask:
            return None
        if negotiation.last_counter_action is None or negotiation.last_counter_amount is None:
            return None
        if negotiation.last_counter_at is None:
            return None
        if self._clock() - negotiation.last_counter_at > COUNTER_REPLAY_WINDOW:
            return None
        return Decision(
            action=NegotiationAction(negotiation.last_counter_action),
            amount=negotiation.last_counter_amount,
            round_number=negotiation.round_number,
        )

    def _remember(self, negotiation: NegotiationSession, ask: int, decision: Decision) -> Decision:
        """Record the answer to `ask` so a retry can replay it."""
        negotiation.last_counter_ask = ask
        negotiation.last_counter_action = decision.action.value
        negotiation.last_counter_amount = decision.amount
        negotiation.last_counter_at = self._clock()
        return decision

    def _accept(
        self,
        session,
        negotiation: NegotiationSession,
        rate: int,
        round_number: int,
    ) -> Decision:
        negotiation.outcome = NegotiationOutcome.ACCEPTED
        negotiation.agreed_rate = rate
        negotiation.current_offer = rate
        negotiation.updated_at = self._clock()

        self._sink.emit(
            session.run_id,
            EventType.NEGOTIATION_CLOSED,
            negotiation_id=negotiation.negotiation_id,
            load_id=negotiation.load_id,
            outcome=NegotiationOutcome.ACCEPTED.value,
            agreed_rate=rate,
            ceiling=negotiation.ceiling,
            rounds=round_number,
        )
        return Decision(action=NegotiationAction.ACCEPT, amount=rate, round_number=round_number)

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _active_or_raise(run_id: str, negotiations: list[NegotiationSession]):
        if not negotiations:
            raise NoActiveNegotiation(f"run {run_id!r} has no negotiation to counter")
        latest = negotiations[-1]
        if not latest.is_open:
            raise NegotiationAlreadyClosed(latest.outcome)
        return latest

    def _require_ceiling(self, load: LoadDetail) -> int:
        ceiling = load.max_buy
        if ceiling is None:
            logger.error(
                "load %s has no MAX_BUY; refusing to negotiate without a ceiling",
                load.load_id,
            )
            raise CeilingUnavailable(f"load {load.load_id} has no MAX_BUY")
        if ceiling < self._increment + self._margin:
            # No room for a single increment under the ceiling, so there is
            # nothing a deterministic ladder could do here.
            raise CeilingUnavailable(
                f"load {load.load_id} ceiling {ceiling} leaves no room to negotiate"
            )
        return ceiling


def _validate_ask(carrier_ask: int | str) -> int:
    """A carrier request must be a positive whole number of dollars.

    Rejecting junk here rather than coercing it matters: `int("2200.50")` raises,
    and silently rounding someone's ask down would change what we agree to pay.
    """
    if isinstance(carrier_ask, bool):
        raise ValueError("carrier_ask must be a number, not a boolean")
    try:
        ask = int(str(carrier_ask).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"carrier_ask must be a whole number, got {carrier_ask!r}") from exc
    if ask <= 0:
        raise ValueError("carrier_ask must be positive")
    return ask
