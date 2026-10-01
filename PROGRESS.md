# Progress log

One checkpoint per phase. Newest at the bottom.

---

## Phase 1 — TMS adapter (2026-10-01)

**Status:** code complete and tested. The live smoke run is **blocked on credentials**
(see Open issues).

### What was built

| File | Purpose |
|---|---|
| `app/config.py` | Settings from env / `.env`; the token is a `SecretStr` so it cannot be logged by accident |
| `app/tms/protocol.py` | Pure wire layer: `encode_request` with sanitisation, `parse_record`/`parse_response`, the exception hierarchy, `redact()` |
| `app/tms/models.py` | `Load`, `LoadDetail`, `BookResult`; `public_dict()` is where `MAX_BUY` is guaranteed to be dropped |
| `app/tms/client.py` | Sockets, deadlines, fault classification, retry policy, ambiguous-booking recovery |
| `tests/fake_tms.py` | Local TCP server replaying the fixtures with injectable faults |
| `scripts/tms_smoke.py` | Read-only probe of the live server, guarded against every command except `DEBUG_ECHO`/`LOAD_QUERY`/`LOAD_GET` |

Environment: Python 3.12.1 in `.venv/`, `pyproject.toml` with pydantic v2,
pydantic-settings and pytest.

### Tests

```
116 passed in 2.46s
```

Coverage by area:

- **Both wire formats.** The manual style (`LD0000045821`, `RATE:0002150`) and the live
  style (`LD00896     `, `RATE:2073    `) parse to the same values. The recorded fixtures
  are parsed end to end in the tests.
- **Every fault class** of the brief against the fake server: no bytes before the deadline
  (`TmsTimeout`), closed before `END` (`TmsPartialResponse`), framing/field violations and
  an unterminated oversized line (`TmsMalformed`), `ERR|CODE:` (`TmsError`), and delayed
  termination (must not burn the deadline).
- **Retry policy.** `AUTH_FAILED`/`MISSING_FIELD`/`UNKNOWN_LOAD`/`UNKNOWN_CMD` are not
  retried; `SERVER_ERROR` and unknown codes are; idempotent commands recover on a second
  attempt.
- **Ambiguous `LOAD_BOOK`.** Response lost and the write landed → `booked_unconfirmed` with
  no reference, and no second booking. Response lost and the write did not land → exactly
  one retry. Two ambiguous attempts → stop and hand to a human. `INVALID_RATE` → never
  retried. `ALREADY_BOOKED` → `booked_unconfirmed`.
- **No request leaks.** The token is redacted in the fake server's request log; validation
  errors never echo the value they rejected.
- **`MAX_BUY` never escapes.** `LoadDetail.public_dict()` drops it, including the
  mis-cased unknown-field path.

The suite runs in ~2.5 s. That took two fixes: `ThreadingTCPServer.block_on_close` and a
short `serve_forever` poll interval were costing 20 s of sleeping across ~40 fake servers.

### Bugs found by the tests, and fixed

1. **`execute()` returned raw wire lines, not parsed records**, so `Load.from_record` was
   handed a string. Parsing now happens per attempt, which also means a framing fault is
   correctly retried for idempotent commands.
2. **`LoadDetail` leaked `MAX_BUY` into `public_dict()`.** The base `from_record` filed it
   as an unknown field, and `extra` is merged into the public payload. This is the one
   invariant the whole design rests on; it is now recomputed against the subclass's field
   list *and* filtered again on the way out.
3. **The client hung on a held-open connection.** The live server does not always terminate
   `END` with `\r\n`, so a complete response sat in the buffer until the 5 s deadline and
   came back as a partial response. The reader now accepts an unterminated trailing `END`.

### Smoke test against the live server

**Blocked.** Every command is rejected:

```
ERR|CODE:AUTH_FAILED|MSG:Invalid or missing auth token
```

The transport is fine — the host is reachable and answers in ~360 ms at the application
layer. The rejection is not on our side:

- A hand-built frame (`CMD:DEBUG_ECHO|AUTH:<token>\r\n`, 45 bytes, written straight to a
  socket with nothing from `app/tms` involved) gets the same `AUTH_FAILED`.
- The `.env` is parsed cleanly: one `TMS_TOKEN` line, no BOM, no `\r`, no `export` prefix,
  no quotes, no surrounding whitespace, and the loaded value is byte-identical to the file.

The same token reportedly answered `AUTH:OK` on 2026-09-30, which points at a rotated or
expired demo token. Consequently the four open questions in `docs/tms-protocol.md` are
**still unanswered**: the real `MAX_RESULTS` ceiling, whether `PICKUP_DT` is the right
filter name, whether `FLATBED` is a real equipment value, and today's padding.

`LOAD_BOOK` was never sent. `scripts/tms_smoke.py` refuses any command outside
`DEBUG_ECHO`, `LOAD_QUERY` and `LOAD_GET`, and `tests/test_smoke_guard.py` enforces that.

### Open issues

1. **TMS token rejected** — blocks the smoke observations and any live verification.
   Re-run `.venv/bin/python scripts/tms_smoke.py --out docs/smoke-<date>.txt` with a fresh
   token. It answers all four questions in one pass.
2. **Timestamp zone unconfirmed** — `DECISIONS.md` §2 assumes UTC. The evidence for that is
   the manual's description of the `LOAD_BOOK` `TIMESTAMP` field, not an observation.
3. **Parse of `MALFORMED` as an error code** — treated as non-retryable, matching the
   documented code list. Not observed on the wire.
4. **Untested against the real fault behaviour** — the fault modes come from the manual's
   table. None has been observed live (the 2026-09-30 run saw none in three calls).

### Not started

Phases 2–4: FMCSA client, OTP, negotiation engine, the HTTP API, Docker and deployment.
