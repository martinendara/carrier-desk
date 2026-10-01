# Fixtures

## `tms_samples.txt`
Real lines captured from the live legacy TMS on 2026-09-30. Authoritative.

## `fmcsa_carrier_recorded.json` — RECORDED
The live FMCSA response for MC 123456, captured on 2026-10-01 from the US
deploy. Transcribed out of that deploy's service log — the warning in
`app/fmcsa/client.py` prints the record it could not parse — and re-wrapped in
the `{"content": [{"carrier": ...}]}` shape.

Two caveats, both deliberate:

- **The wrapper is still an assumption.** What the log carried was the carrier
  object; the wrapper is what our own parser strips on the way in. `content` is
  what it looked like, and `_find_carrier` still tolerates the other shapes
  because this response did not settle that question.
- **`ein` is a placeholder.** The real value was redacted before the payload
  left the operator's clipboard, and the service strips the field before
  writing anything down anyway.

Everything the parser reads is in here: `allowedToOperate`, `legalName`,
`dotNumber` (an integer, not the string the documentation describes),
`statusCode`, `commonAuthorityStatus`, `contractAuthorityStatus`, `oosDate`,
and the two `bipdInsurance*` fields. There is no authority date in the record,
which is why `authority_age_days` is `None` for every carrier.

## The variants are built, not stored
Every other carrier in the tests is this payload with named fields changed —
`fmcsa_payload()` in `tests/conftest.py`, and its `ELIGIBLE_OVERRIDES`. Six
copies of a fifty-field record would drift apart; a named delta says which
single key makes a carrier bookable, or which one refuses them.

`fmcsa_carrier_not_found.json` stays a file: it is a shape (an empty `content`)
rather than a variation on a carrier.
