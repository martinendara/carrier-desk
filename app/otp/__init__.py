"""Email OTP: issue to the contact on file, verify against an HMAC digest."""

from app.otp.security import (
    CODE_DIGITS,
    WeakSecretError,
    generate_code,
    hash_code,
    mask_email,
    verify_code,
)
from app.otp.service import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_RESENDS,
    DEFAULT_TTL_SECONDS,
    DispatchStatus,
    OtpDispatch,
    OtpFailure,
    OtpService,
    OtpVerification,
)

__all__ = [
    "CODE_DIGITS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_RESENDS",
    "DEFAULT_TTL_SECONDS",
    "DispatchStatus",
    "OtpDispatch",
    "OtpFailure",
    "OtpService",
    "OtpVerification",
    "WeakSecretError",
    "generate_code",
    "hash_code",
    "mask_email",
    "verify_code",
]
