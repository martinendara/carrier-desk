# Fixtures

## `tms_samples.txt`
Real lines captured from the live legacy TMS on 2026-09-30. Authoritative.

## `fmcsa_*.json` — SYNTHETIC, NOT RECORDED
**None of these came from FMCSA.** The domain returns 403 from Argentina, so no
real response has been recorded yet. These files were written from the public
API documentation to give the parser something to be tested against, and every
key in them is a guess.

Treat them as a statement of what we *expect*, not of what FMCSA sends. When a
real response is finally captured from a US region, replace these files and
check every name in `app/fmcsa/client.py` that is marked `TODO: verify`.

The key names most likely to be wrong: `allowedToOperate`, `outOfService`,
`statusCode`, and the authority date (`addDate`). A wrong date key does not fail
loudly — it makes `authority_age_days` come back `None`, which routes the lead
to `Review`. That is the safe direction, but it means the bug hides as a
business outcome rather than as an error.
