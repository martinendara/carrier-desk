"""OTP: generation, storage, masking, the attempt budget and the resend.

The code is treated as a secret throughout: several of these tests exist only to
assert that it does not turn up where it should not.
"""

from __future__ import annotations

import pytest
from conftest import OTP_SECRET, TickClock
from helpers import FakeMailer

from app.carriers import directory
from app.events import EventType
from app.otp import (
    DispatchStatus,
    OtpFailure,
    OtpService,
    WeakSecretError,
    generate_code,
    hash_code,
    mask_email,
    verify_code,
)

#: A seeded MC that has a contact, and one that does not.
KNOWN_MC = "123456"
UNKNOWN_MC = "999999"


def make_service(store, tick: TickClock, events=None, **overrides) -> OtpService:
    options = {
        "secret": OTP_SECRET,
        "mailer": FakeMailer(),
        "ttl_seconds": 300,
        "max_attempts": 3,
        "max_resends": 1,
        "clock": tick,
        "sink": events,
    }
    options.update(overrides)
    return OtpService(store, **options)


# --- masking ------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("m.halstead@gmail.com", "m***@gmail.com"),
        ("a@b.co", "a***@b.co"),
        ("dispatch@ridgelinefreight.com", "d***@ridgelinefreight.com"),
        ("no-at-sign", "***"),
        ("trailing@", "***"),
        ("", "***"),
    ],
)
def test_masking_keeps_the_first_character_and_the_domain(email, expected):
    assert mask_email(email) == expected


def test_masking_never_leaks_the_local_part():
    masked = mask_email("dispatch@ridgelinefreight.com")

    assert "ispatch" not in masked
    assert "ridgelinefreight.com" in masked


# --- generation and storage ---------------------------------------------


def test_codes_are_six_digits():
    for _ in range(50):
        code = generate_code()
        assert len(code) == 6
        assert code.isdigit()


def test_codes_are_not_the_same_every_time():
    assert len({generate_code() for _ in range(200)}) > 100


def test_a_short_code_is_refused():
    with pytest.raises(ValueError):
        generate_code(digits=3)


def test_a_code_verifies_against_its_own_digest():
    digest = hash_code(OTP_SECRET, "run-1", "123456")

    assert verify_code(OTP_SECRET, "run-1", "123456", digest)
    assert not verify_code(OTP_SECRET, "run-1", "123457", digest)


def test_a_digest_is_bound_to_its_run():
    """A code captured from one call is useless in another."""
    digest = hash_code(OTP_SECRET, "run-1", "123456")

    assert not verify_code(OTP_SECRET, "run-2", "123456", digest)


def test_a_different_secret_cannot_verify():
    digest = hash_code(OTP_SECRET, "run-1", "123456")

    assert not verify_code(b"a-completely-different-secret!!", "run-1", "123456", digest)


def test_the_stored_value_is_not_the_code():
    digest = hash_code(OTP_SECRET, "run-1", "123456")

    assert "123456" not in digest


# --- sending ------------------------------------------------------------


def test_send_reaches_the_contact_on_file(store, tick):
    service = make_service(store, tick)
    dispatch = service.send("run-1", KNOWN_MC)

    assert dispatch.status is DispatchStatus.SENT
    assert dispatch.masked_destination == mask_email(directory.lookup(KNOWN_MC).email)
    assert service._mailer.sent[0].to == directory.lookup(KNOWN_MC).email


def test_the_agent_view_carries_no_code_and_no_address(store, tick):
    dispatch = make_service(store, tick).send("run-1", KNOWN_MC)

    assert dispatch.agent_view == {"masked_destination": dispatch.masked_destination}
    assert not hasattr(dispatch, "code")
    assert not hasattr(dispatch, "destination")


def test_the_email_carries_the_code_for_the_carrier(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)

    email = service._mailer.sent[0]
    assert email.to == directory.lookup(KNOWN_MC).email
    assert email.subject == "Your HappyRobot Logistics verification code"
    assert service._mailer.last_code in email.body


def test_the_dispatch_repr_does_not_leak_the_code(store, tick):
    """A stray log of the object must not print the code."""
    service = make_service(store, tick)
    dispatch = service.send("run-1", KNOWN_MC)

    assert service._mailer.last_code not in repr(dispatch)


def test_only_the_digest_is_stored(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)

    challenge = store.get("run-1").otp
    assert service._mailer.last_code not in challenge.code_hmac


def test_an_mc_with_no_contact_is_routed_to_onboarding(store, tick):
    """No contact on file means no code is generated, and no guessing."""
    dispatch = make_service(store, tick).send("run-1", UNKNOWN_MC)

    assert dispatch.status is DispatchStatus.ROUTED_ONBOARDING
    assert dispatch.agent_view == {"result_code": "routed_onboarding"}
    assert store.get("run-1").otp is None


def test_the_code_never_reaches_the_event_log(store, tick, events):
    service = make_service(store, tick, events)
    service.send("run-1", KNOWN_MC)

    assert service._mailer.last_code not in "".join(
        str(event.payload) for event in events.events
    )
    assert events.of_type(EventType.OTP_SENT)[0].payload["destination"] == mask_email(
        directory.lookup(KNOWN_MC).email
    )


def test_a_failed_email_is_send_failed_and_issues_nothing(store, tick):
    service = make_service(store, tick, mailer=FakeMailer(fail=True))

    dispatch = service.send("run-1", KNOWN_MC)

    assert dispatch.status is DispatchStatus.SEND_FAILED
    assert dispatch.result_code == "otp_failed"
    assert store.get("run-1").otp is None


# --- verifying ----------------------------------------------------------


def test_the_right_code_verifies(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)

    result = service.verify("run-1", service._mailer.last_code)

    assert result.verified
    assert result.agent_view == {"verified": True, "attempts_left": 3}
    assert result.result_code is None


def test_a_wrong_code_costs_an_attempt(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)

    first = service.verify("run-1", "000000")
    second = service.verify("run-1", "000000")

    assert not first.verified and first.attempts_left == 2
    assert not second.verified and second.attempts_left == 1


def test_three_wrong_codes_lock_the_session(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)

    for _ in range(3):
        result = service.verify("run-1", "000000")

    assert result.failure is OtpFailure.LOCKED
    assert result.attempts_left == 0
    assert store.get("run-1").otp.locked


def test_a_locked_session_refuses_the_right_code(store, tick):
    """The lock is absolute: guessing wrong three times ends the OTP path."""
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    for _ in range(3):
        service.verify("run-1", "000000")

    result = service.verify("run-1", service._mailer.last_code)

    assert not result.verified
    assert result.failure is OtpFailure.LOCKED


def test_a_malformed_code_still_costs_an_attempt(store, tick):
    """Probing for the expected length must not be free."""
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)

    result = service.verify("run-1", "abc")

    assert not result.verified
    assert result.attempts_left == 2


def test_an_expired_code_is_refused(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    tick.advance(minutes=5)

    result = service.verify("run-1", service._mailer.last_code)

    assert not result.verified
    assert result.failure is OtpFailure.EXPIRED


def test_a_code_is_still_good_just_before_it_expires(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    tick.advance(minutes=4, seconds=59)

    assert service.verify("run-1", service._mailer.last_code).verified


def test_verifying_twice_is_idempotent(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    service.verify("run-1", service._mailer.last_code)

    again = service.verify("run-1", "000000")

    assert again.verified


def test_verifying_without_a_challenge_is_refused(store, tick):
    store.get_or_create("run-1")

    result = make_service(store, tick).verify("run-1", "123456")

    assert not result.verified
    assert result.failure is OtpFailure.NO_CHALLENGE
    assert result.result_code == "otp_failed"


def test_a_code_from_another_call_does_not_work(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    first_code = service._mailer.last_code
    service.send("run-2", KNOWN_MC)

    result = service.verify("run-2", first_code)

    assert not result.verified
    assert result.failure is OtpFailure.MISMATCH


# --- resending ----------------------------------------------------------


def test_a_resend_issues_a_new_code_and_retires_the_old_one(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    first_code = service._mailer.last_code
    second = service.resend("run-1")
    second_code = service._mailer.last_code

    assert second.status is DispatchStatus.SENT
    assert second_code != first_code
    assert not service.verify("run-1", first_code).verified
    assert service.verify("run-1", second_code).verified


def test_only_one_resend_is_allowed(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    service.resend("run-1")

    third = service.resend("run-1")

    assert third.status is DispatchStatus.RESEND_LIMIT_REACHED
    assert third.result_code == "otp_failed"


def test_calling_send_twice_spends_the_resend_budget(store, tick):
    """Otherwise the one-resend limit is bypassable by hammering send."""
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    service.send("run-1", KNOWN_MC)

    third = service.send("run-1", KNOWN_MC)

    assert third.status is DispatchStatus.RESEND_LIMIT_REACHED


def test_a_resend_does_not_refund_attempts(store, tick):
    """Three guesses per call, no matter how many codes were issued."""
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    service.verify("run-1", "000000")
    service.verify("run-1", "000000")

    service.resend("run-1")
    result = service.verify("run-1", "000000")

    assert result.attempts_left == 0
    assert result.failure is OtpFailure.LOCKED


def test_a_locked_session_cannot_be_resent(store, tick):
    """A new code would hand the caller three more guesses."""
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    for _ in range(3):
        service.verify("run-1", "000000")

    assert service.resend("run-1").status is DispatchStatus.LOCKED


def test_a_verified_session_cannot_be_resent(store, tick):
    service = make_service(store, tick)
    service.send("run-1", KNOWN_MC)
    service.verify("run-1", service._mailer.last_code)

    assert service.resend("run-1").status is DispatchStatus.ALREADY_VERIFIED


def test_a_resend_without_a_prior_send_is_refused(store, tick):
    store.get_or_create("run-1")

    from app.sessions import SessionNotFound

    with pytest.raises(SessionNotFound):
        make_service(store, tick).resend("run-1")


# --- configuration ------------------------------------------------------


def test_a_missing_secret_is_refused(store, tick):
    with pytest.raises(WeakSecretError):
        make_service(store, tick, secret=b"")


def test_a_short_secret_is_refused(store, tick):
    """A guessable key makes the stored digest worthless."""
    with pytest.raises(WeakSecretError):
        make_service(store, tick, secret=b"short")


def test_the_events_record_the_whole_lifecycle(store, tick, events):
    service = make_service(store, tick, events)
    service.send("run-1", KNOWN_MC)
    service.verify("run-1", "000000")
    service.verify("run-1", service._mailer.last_code)

    assert len(events.of_type(EventType.OTP_SENT)) == 1
    assert len(events.of_type(EventType.OTP_FAILED)) == 1
    assert len(events.of_type(EventType.OTP_VERIFIED)) == 1


def test_a_sink_is_optional(store, tick):
    """Nothing has to be optional upstream just to keep this testable."""
    service = OtpService(store, secret=OTP_SECRET, mailer=FakeMailer(), clock=tick)

    assert service.send("run-1", KNOWN_MC).status is DispatchStatus.SENT


def test_events_can_be_recorded_into_a_real_sink(store, tick, tmp_path):
    from app.events import JsonlEventSink

    sink = JsonlEventSink(tmp_path / "events.jsonl")
    make_service(store, tick, sink).send("run-1", KNOWN_MC)

    assert [event.type for event in sink.read_all()] == [EventType.OTP_SENT]
