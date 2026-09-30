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
