# Legacy TMS — Protocol reference (consolidated)

Source: candidate handbook (FORM-9100, REV 1.0, issued 2026-09-30) plus live observations on 2026-09-30. **When the manual and the live server disagree, the live server wins.**

## Transport
| Property | Value |
|---|---|
| Transport | TCP |
| Encoding | ASCII |
| Line terminator | `\r\n` |
| Max frame | 4096 bytes including terminator |
| Idle timeout | 30 s, server closes |
| Lifecycle | One request per connection; server closes after responding. No reuse. |

Host and port come from env (`TMS_HOST`, `TMS_PORT`). Token from `TMS_TOKEN`.

## Request
`CMD:<command>|AUTH:<token>|<FIELD>:<VALUE>|...\r\n`
- `CMD` first, `AUTH` on every request. Upper-case field names.
- Values must not contain `|` or `\r\n`. Unknown fields are accepted and silently discarded (so a misspelled filter looks like "no filter").

## Response
- Success: zero or more record lines, then `END\r\n`.
- Error: single line `ERR|CODE:<code>|MSG:<msg>\r\n`.
- Values are space-padded on the right. Field order is stable per server build but **not** across builds.

## Error codes seen (not exhaustive)
`AUTH_FAILED`, `UNKNOWN_CMD`, `MISSING_FIELD`, `UNKNOWN_LOAD`, `ALREADY_BOOKED`, `INVALID_RATE`, `MALFORMED`, `SERVER_ERROR`.

## Commands

### LOAD_QUERY
Filters (at least one required): `ORIG_CITY`, `ORIG_STATE`, `ORIG_ZIP`, `DEST_CITY`, `DEST_STATE`, `DEST_ZIP`, `EQTYPE`, pickup date (field name not confirmed — test `PICKUP_DT`). `MAX_RESULTS` caps results; the server applies its own undisclosed ceiling.
- City matching is forgiving; state and ZIP are exact.
- Equipment values are short upper-case identifiers (`DRY_VAN`, `REEFER`, likely `FLATBED` — confirm). An unknown value returns no error and no records.
- Record fields: `LOAD_ID, ORIG_CITY, ORIG_STATE, ORIG_ZIP, DEST_CITY, DEST_STATE, DEST_ZIP, PICKUP_DT, EQTYPE, RATE, MILES, STATUS`.
- No filter → `ERR|CODE:MISSING_FIELD|MSG:At least one filter required`.

### LOAD_GET
`LOAD_ID` required. Returns one record + `END`. Extra fields vs. query: `DELIVERY_DT, WEIGHT, COMMODITY, PIECES, DIMS, NOTES, MAX_BUY`.
- `NOTES` is the widest field; blank notes are all spaces → empty string after trim.
- `COMMODITY` and `DIMS` are free text.
- `MAX_BUY` appears only for flagged tokens. **Our token is flagged** (observed).
- Returns records regardless of `STATUS` (booked or past loads still resolve).
- Unknown id → `ERR|CODE:UNKNOWN_LOAD|MSG:load not found`.

### LOAD_BOOK
`LOAD_ID`, `MC_NUM`, `AGREED_RATE` required.
- Success: `LOAD_ID:...|BOOKING_REF:...|STATUS:BOOKED|TIMESTAMP:<UTC YYYYMMDDHHMMSS>` + `END`. `BOOKING_REF` is opaque.
- Bookings are keyed to the token and **monotonic**: after `ALREADY_BOOKED` for a load on a token, it stays booked for that token. Other tokens unaffected.
- `INVALID_RATE` covers more than obvious cases; the accepted range is undocumented and must be bounded by observation (likely relative to `MAX_BUY` and/or `RATE`).
- Missing field → `MISSING_FIELD`. Unknown load → `UNKNOWN_LOAD`.
- **Every successful booking permanently consumes a load for this token.**

### DEBUG_ECHO
Diagnostic. `MSG` echoed verbatim; `FIELDS_PARSED` counts every accepted pair including `CMD` and `AUTH`. Bypasses fault injection, so it says nothing about operational health. Useful to verify the encoder.

## Fault behavior (operational commands only)
Faults are not signaled. Detect them from the wire.
| Category | Wire symptom |
|---|---|
| Timeout | Request read, no response; closed later by idle timeout |
| Partial response | Valid prefix, connection closed without `END`; last record may be cut |
| Malformed response | Extra delimiters, unterminated lines, values exceeding widths |
| Delayed termination | Complete response, connection held open afterward |
The server may add categories without notice.

## Observed vs. manual (2026-09-30)
| Aspect | Manual sample | Live server |
|---|---|---|
| Load ID | `LD0000045821` | `LD00896     ` (shorter, right-padded) |
| Numbers | `RATE:0002150` (zero-padded) | `RATE:2073    ` (right-padded with spaces) |
| `MAX_BUY` vs `RATE` | 1950 vs 2150 (ceiling below posted) | 2528 vs 2073 (ceiling above posted) |
| Faults | — | none observed in 3 query calls |

## Observed vs. manual (2026-10-01) — BLOCKED, token rejected

`scripts/tms_smoke.py` could not complete: **every request is rejected with
`AUTH_FAILED`**, so none of the open questions below were answered.

| Aspect | Manual | Observed today |
|---|---|---|
| Transport | TCP, one request per connection | reachable, round trip ≈ 360 ms to `tramway.proxy.rlwy.net:17159` |
| Auth | `AUTH:<token>` on every request | `ERR\|CODE:AUTH_FAILED\|MSG:Invalid or missing auth token` for every command |
| `MAX_RESULTS` ceiling | undisclosed | **not measured** |
| `PICKUP_DT` filter name | "not confirmed — test `PICKUP_DT`" | **not measured** |
| `FLATBED` equipment value | "likely — confirm" | **not measured** |
| Live padding | full detail has `MAX_BUY` | **not measured**; the 2026-09-30 samples above are still the only ones |

### What was ruled out
The rejection is **not** on our side. A hand-built frame — 45 bytes,
`CMD:DEBUG_ECHO|AUTH:<token>\r\n`, written straight to a socket with nothing from
`app/tms` involved — gets the same `AUTH_FAILED`. The `.env` parse is also clean:
one `TMS_TOKEN` line, no BOM, no `\r`, no `export` prefix, no quotes, no surrounding
whitespace, and the loaded value is byte-identical to the file.

### What is left
The token value itself is being refused. It worked on 2026-09-30 from another
terminal, which points at a rotated or expired demo token rather than a bad paste.
Re-run `.venv/bin/python scripts/tms_smoke.py --out docs/smoke-<date>.txt` with a
fresh token; the script answers all four open questions in one pass.

Full output: `docs/smoke-2026-10-01.txt`.

## Observed vs. manual (2026-10-03) — framing confirmed, auth still refused

`scripts/tms_connectivity.py` re-probed the live server from the same machine with raw
socket I/O, printing every response line with `repr()`. The token is still refused, but the
probing settled **three framing questions that were open**, because a malformed frame gets a
different error than a refused token — which proves the server parses before it authenticates.

| Frame sent | Answer |
|---|---|
| `CMD:DEBUG_ECHO\|AUTH:<token>\|MSG:diag\r\n` | `ERR\|CODE:AUTH_FAILED\|MSG:Invalid or missing auth token` |
| `CMD:DEBUG_ECHO\|MSG:diag\r\n` — **no AUTH field at all** | same `AUTH_FAILED` |
| `CMD:NOT_A_REAL_COMMAND\|AUTH:<token>\|MSG:diag\r\n` | same `AUTH_FAILED` |
| `CMD:DEBUG_ECHO\|AUTH:<wrong-token>\|MSG:diag\r\n` | same `AUTH_FAILED` |
| `CMD:DEBUG_ECHO\|AUTH:<token>\n` — **LF, no CR** | **no bytes at all**, until the 5 s deadline |
| `AUTH:<token>\|CMD:DEBUG_ECHO\r\n` — **AUTH before CMD** | **`ERR\|CODE:MALFORMED\|MSG:Malformed frame`** |

**Confirmed.** `\r\n` is required — an LF-only frame gets no answer, so the server
accumulates until it sees the CR. `CMD` must come first: putting `AUTH` first is rejected
as `MALFORMED`, not as a bad token. Both were assumptions taken from the manual until now;
`docs/tms-protocol.md` §Request is right as written.

**Also observed.** Authentication is checked **before** the command is validated: an
unknown command carrying a token answers `AUTH_FAILED`, never `UNKNOWN_CMD`. And "invalid"
and "missing" are one case on the wire — a frame with no `AUTH` field and a frame with the
wrong token are refused with the same sentence, so the message cannot be read as a
statement about the token specifically.

**Still refused.** Our token, and a deliberately wrong one, are indistinguishable from the
outside. Either the credential is stale or the server accepts none at all; both are fixed by
re-issuing it. The 2026-09-30 samples remain the only successful live exchange.

**Untouched.** No `LOAD_BOOK` was sent, and the four open questions above
(`MAX_RESULTS` ceiling, `PICKUP_DT`, `FLATBED`, today's padding) are **still unanswered** —
they need a working token, which is exactly what the auth step blocks.

Full output: `docs/tms-connectivity-2026-10-03.md`.

## Live samples (2026-09-30)
```
CMD:DEBUG_ECHO|AUTH:<token>
→ ECHO|AUTH:OK|FIELDS_PARSED:2|MSG:
→ END

CMD:LOAD_QUERY|AUTH:<token>|EQTYPE:DRY_VAN|MAX_RESULTS:5
→ LOAD_ID:LD00859     |ORIG_CITY:Rochester                     |ORIG_STATE:NY|ORIG_ZIP:14604|DEST_CITY:Long Beach                    |DEST_STATE:CA|DEST_ZIP:90802|PICKUP_DT:20261003235900|EQTYPE:DRY_VAN   |RATE:5126    |MILES:2265  |STATUS:OPEN    
→ LOAD_ID:LD00896     |ORIG_CITY:Fort Lauderdale               |ORIG_STATE:FL|ORIG_ZIP:33301|DEST_CITY:Pittsburgh                    |DEST_STATE:PA|DEST_ZIP:15219|PICKUP_DT:20261006221800|EQTYPE:DRY_VAN   |RATE:2073    |MILES:989   |STATUS:OPEN    
→ END   (5 records total; full output in tests/fixtures/tms_samples.txt)

CMD:LOAD_GET|AUTH:<token>|LOAD_ID:LD00896
→ LOAD_ID:LD00896     |ORIG_CITY:Fort Lauderdale               |ORIG_STATE:FL|ORIG_ZIP:33301|DEST_CITY:Pittsburgh                    |DEST_STATE:PA|DEST_ZIP:15219|PICKUP_DT:20261006221800|DELIVERY_DT:20261009101800|EQTYPE:DRY_VAN   |RATE:2073    |WEIGHT:24137   |COMMODITY:Lumber                        |PIECES:30    |MILES:989   |DIMS:50ft x 8ft x 9ft              |NOTES:No paperwork at pickup. Call dispatch on arrival.                                                                       |STATUS:OPEN    |MAX_BUY:2528    
→ END

CMD:LOAD_QUERY|AUTH:<token>            (no filter)
→ ERR|CODE:MISSING_FIELD|MSG:At least one filter required
```
