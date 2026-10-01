# CLAUDE.md — Inbound Carrier Sales Automation (HappyRobot FDE challenge)

You are the build agent for this repository. Read this file fully before writing code. Work **phase by phase**, stop at each checkpoint, and report back. The human (Martín) orchestrates and audits; he must be able to explain every design decision in a follow-up interview, so every non-obvious choice gets a short entry in `DECISIONS.md`.

## 1. What we are building

A freight brokerage wants a voice agent (built on the HappyRobot platform) to handle the first leg of inbound carrier calls: verify the carrier with FMCSA, send an OTP, pitch a matching load, negotiate the rate (max three counter rounds), book it, and hand off to a senior rep (mocked).

**This repository contains the backend service ("carrier-desk")** that the HappyRobot workflow calls as HTTP tools. It owns all business logic and all integrations. The voice agent only talks and relays.

```
Carrier (web call)
   │
HappyRobot workflow (voice agent, SMS, Twin, Apps)
   │  HTTPS + API key
carrier-desk (this repo, FastAPI, Docker)
   ├── Legacy TMS  — raw TCP, line protocol, faulty
   └── FMCSA API   — carrier authority
```

## 2. Non-negotiable design principles

1. **The agent never sees `MAX_BUY`.** No endpoint returns it, no log line sent to the agent contains it. Negotiation decisions are made server-side. The agent cannot leak what it does not have.
2. **The agent never decides security outcomes.** OTP verification and booking happen server-side. Booking uses the rate stored in the server's negotiation session, never a rate passed by the agent.
3. **Parse the TMS by field name, never by position or width.** The live server already differs from the manual (see `docs/tms-protocol.md`, section "Observed vs. manual").
4. **A TMS response without `END` is a failure**, even if it contains valid-looking records. A truncated search must never be presented as "few loads available".
5. **Never trust carrier-provided input into the TMS protocol.** Validate and sanitize every value before building a request line (no `|`, `\r`, `\n`, non-ASCII).
6. **No secrets in the repo.** Everything comes from environment variables. `.env` is gitignored; `.env.example` documents the variables.

## 3. Stack and layout

- Python 3.12, FastAPI, uvicorn, httpx, pydantic v2, pytest. Standard library `socket` for the TMS (no extra dependency).
- In-memory state with TTL for OTP and negotiation sessions (single-instance POC; justify in `DECISIONS.md` and note Redis as the production path).

```
app/
  main.py              # FastAPI app, routers, health
  config.py            # settings from env
  security.py          # API key dependency (header: X-API-Key)
  tms/
    protocol.py        # encode request line, parse lines → dicts
    client.py          # socket I/O, deadlines, retries, fault detection
    models.py          # Load, LoadDetail (pydantic), conversions
  fmcsa/client.py      # authority check
  otp/service.py       # generate, store (hashed), verify, attempts
  negotiation/engine.py# deterministic concession strategy
  carriers/directory.py# seeded contact-on-file directory (demo)
tests/
  fixtures/            # recorded real TMS lines + FMCSA JSON
  fake_tms.py          # local TCP server replaying fixtures with injected faults
  test_protocol.py test_client_faults.py test_negotiation.py test_otp.py test_api.py
docs/tms-protocol.md   # consolidated protocol reference (read it)
DECISIONS.md           # one entry per design decision: problem, choice, alternative, why
PROGRESS.md            # checkpoint log, updated at the end of each phase
Dockerfile, docker-compose.yml, .env.example, README.md
```

## 4. TMS client specification

Read `docs/tms-protocol.md` first.

**Transport:** new TCP connection per request. Connect timeout 3 s. Total read deadline 5 s (configurable) — shorter than the server's 30 s idle timeout because a carrier on the road will not wait.

**Encoding:** `CMD:<cmd>|AUTH:<token>|KEY:VALUE...\r\n`, CMD first, ASCII, frame ≤ 4096 bytes. Reject any value containing `|`, `\r`, `\n` or non-ASCII before sending.

**Reading:** read until a line equal to `END` (after trimming) or a line starting with `ERR|`. Then **close the socket yourself** (handles "delayed termination").

**Parsing:** split a line on `|`; split each token on the **first** `:`; trim whitespace on keys and values. Keep unknown fields. A token without `:` → malformed. Convert numeric fields (`RATE`, `MAX_BUY`, `MILES`, `WEIGHT`, `PIECES`) with `int()` after trimming; strip leading zeros implicitly. Dates `YYYYMMDDHHMMSS` → ISO 8601 (assume UTC unless observed otherwise; note it in `DECISIONS.md`). Blank `NOTES` → empty string is correct.

**Validation per record:** required fields present (`LOAD_ID`, `ORIG_CITY`, `ORIG_STATE`, `DEST_CITY`, `DEST_STATE`, `PICKUP_DT`, `EQTYPE`, `RATE`, `STATUS`) and numeric fields parse. Otherwise → malformed.

**Fault detection → exceptions:**
| Wire symptom | Classification |
|---|---|
| No bytes before deadline | `TmsTimeout` |
| Connection closed before `END`/`ERR` | `TmsPartialResponse` |
| Framing/field violations, frame > 4096 | `TmsMalformed` |
| `ERR|CODE:X` | `TmsError(code)` |

**Retry policy:**
- `LOAD_QUERY`, `LOAD_GET` (idempotent): retry on `TmsTimeout`, `TmsPartialResponse`, `TmsMalformed`, `TmsError(SERVER_ERROR)` and unknown error codes. Up to 3 attempts, exponential backoff with jitter (~0.3 s, 0.8 s, 1.5 s). Never retry `AUTH_FAILED`, `MISSING_FIELD`, `UNKNOWN_LOAD`, `UNKNOWN_CMD`.
- `LOAD_BOOK` (not idempotent): **no blind retry.** On a transport fault, call `LOAD_GET` and inspect `STATUS`. If still `OPEN`, retry `LOAD_BOOK` once. If `ALREADY_BOOKED` is returned after our own ambiguous attempt, treat as booked-by-us (bookings are keyed per token and monotonic) and return `booking_ref: null` with `status: booked_unconfirmed` for the senior rep to confirm. Never retry `INVALID_RATE`.
- Every attempt is logged (command, attempt number, fault class, latency) **without** the token.

**Warning:** each successful `LOAD_BOOK` permanently consumes a load for this token. Never call the real `LOAD_BOOK` in automated tests. Use the fake server.

## 5. FMCSA client

- Endpoint (to confirm from a US machine): `https://mobile.fmcsa.dot.gov/qc/services/carriers/docket-number/{mc}?webKey={key}`.
- **FMCSA blocks requests from Argentina (403 on the whole domain).** Live calls only work from a US region. Tests must use a recorded fixture in `tests/fixtures/fmcsa_*.json`. Until a real response is recorded, build the parser defensively and mark the expected fields as `TODO: verify` in code and in `PROGRESS.md`.
- Eligibility: carrier found, allowed to operate, authority active, not out of service. Return `{eligible, legal_name, dot_number, reason}`. On FMCSA outage: fail closed (not eligible, reason `verification_unavailable`) — justify in `DECISIONS.md`.
- Validate MC before calling: digits only, 1–8 characters.

## 6. OTP

**Email only for the POC.** No SMS provider is wired up, and the contact on file has an address.

- The carrier directory (`carriers/directory.py`) holds a **contact email** per MC. The code goes to that address and nowhere else — never to a destination the caller dictates.
- 6 digits from `secrets`, stored as **HMAC-SHA256 keyed with a server-side secret**, not a plain hash. Six digits is a million possibilities: an unsalted digest is brute-forced instantly, and a leaked table would hand over every live code.
- TTL 5 minutes, 3 verification attempts then the session is locked, 1 resend.
- `send` returns **two views**: `{code, destination}` for the platform's delivery node, and `{masked_destination}` for the agent (`m***@gmail.com`). The platform keeps them apart (Tool Call Result); this service documents which field is for whom, and only the masked one is agent-facing.
- An MC with **no contact on file** gets no OTP at all: the call is routed to onboarding (`routed_onboarding`).
- No bypass endpoint, no debug code, no "admin override".

## 7. Negotiation engine (deterministic)

`C = MAX_BUY`. It never leaves the server: not in an agent-facing response, not in a log line the agent can see.

**Opening offer `O`**, computed when the negotiation is created from a `LoadDetail`:
- `RATE < C` → `O = floor25(RATE)`
- otherwise → `O = floor25(0.90 × C)`

`floor25` rounds **down** to a multiple of 25.

**Urgent** = pickup less than 24 h from now, in the timezone fixed by `DECISIONS.md` §2 (UTC).

**Concession steps**, as a fraction of the gap `C − O`:

| Round | Normal | Urgent |
|---|---|---|
| 1 | 40% | 60% |
| 2 | 75% | 90% |

Each step is `floor25`-ed, capped at `C − 25`, and never below the current offer + 25. If the cap does not allow the increment, the step stays at the cap.

**A round is one carrier request, counted by the server.** For each request:
1. request ≤ current offer → **accept** at the request.
2. rounds 1 and 2: request ≤ step → **accept** at the request; otherwise **counter** at the step.
3. round 3: request ≤ `C` → **accept** at the request; otherwise **close** with no transfer, and record `gap = request − C`.

Never counter above the request. Never offer more than `C − 25`. A fourth request is refused.

**Agent-facing response:** `{action: accept|counter|close, amount}`. Nothing else — no `C`, no gap, no steps.

## 8. HTTP API (tools for the voice agent)

All endpoints require `X-API-Key`. Responses are short and agent-friendly (the agent speaks them).

**Every call carries a `run_id`** that the platform injects as a fixed parameter into all tools. The agent never passes `otp_session_id`, `negotiation_id` or a round number — the server resolves all of that from the `run_id`. Per `run_id` the server keeps the OTP state, the loads shown during that call, the active negotiation and the round counter.

- A `load_id` can only be negotiated if it was **shown during that same call**.
- At most **2 negotiations per call**: the original load plus one alternative.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (no auth) |
| POST | `/carriers/verify` | `{run_id, mc_number}` → eligibility |
| POST | `/otp/send` | `{run_id, mc_number}` → `{masked_destination}` for the agent, `{code, destination}` for the platform |
| POST | `/otp/verify` | `{run_id, code}` → `{verified, attempts_left}` |
| POST | `/loads/search` | `{run_id, filters...}` → up to 3 loads, no `MAX_BUY` |
| GET | `/loads/{load_id}` | detail for the pitch, no `MAX_BUY` |
| POST | `/negotiations` | `{run_id, load_id}` → `{offer}` (requires verified OTP) |
| POST | `/negotiations/counter` | `{run_id, carrier_ask}` → `{action, amount}` |
| POST | `/bookings` | `{run_id}` → books via TMS with the rate stored in the session |
| POST | `/handoff` | `{run_id}` → mocked senior-rep queue |

Gating: search, negotiation and booking require a verified OTP session for the same MC.

**Result codes (closed set):** `booked`, `booking_pending`, `negotiation_failed`, `no_match`, `otp_failed`, `routed_onboarding`, `fmcsa_rejected:<reason>`. Nothing outside this list; `fmcsa_rejected` carries its reason as a suffix.

**Lead qualification** (pure function, no LLM). Evaluated in this order:
1. authority age < 90 days, **or** unknown, **or** zero matching loads → `Review`
2. otherwise ≥ 3 matching loads → `Hot`
3. otherwise → `Warm`

The FMCSA fields this reads stay marked `TODO: verify` until a real response is recorded.

## 9. Event log

The service records **what it decided**; the platform records the conversation. Two audiences, two stores — so the events are ours to shape.

- `EventSink` interface with an in-memory implementation and a JSONL file implementation for now. The Twin adapter comes later, because its authentication is not confirmed.
- Event shape: `{run_id, ts, type, payload}`.
- Types: `fmcsa_checked`, `otp_sent`, `otp_verified`, `otp_failed`, `search_performed` (with the filters and the result count, **including when it is 0**), `load_pitched`, `negotiation_opened`, `negotiation_round`, `negotiation_closed`, `booking_attempted`, `booking_result`, `handoff_created`, `lead_created`.
- Events **may** contain `C`, because they are internal and never travel back to the agent. A test asserts that no agent-facing response contains a `C` value.

## 10. Build phases and checkpoints

**Phase 1 — TMS adapter (highest risk).** `protocol.py`, `client.py`, `models.py`, `fake_tms.py` (modes: normal, timeout, partial, malformed, delayed), tests for every fault class plus fixtures from real samples. Checkpoint: `pytest` green; a script `scripts/tms_smoke.py` runs one real `LOAD_QUERY` and one `LOAD_GET` against the live server using `.env`. Stop and report.

**Phase 2 — FMCSA, OTP, negotiation, onboarding, events, run sessions (units only, no HTTP).** `app/fmcsa`, `app/otp`, `app/negotiation`, `app/onboarding`, `app/events`, plus the session keyed by `run_id`. FMCSA runs against fixtures (it blocks Argentina) and fails closed on outage. Checkpoint: `pytest` green; `DECISIONS.md` and `docs/walkthrough.md` updated; commit locally on main; report and stop.

**Phase 3 — HTTP API + auth.** Wire everything, gating, error mapping, agent-friendly messages. Checkpoint: API tests green; `curl` examples in `README.md`. Stop and report.

**Phase 4 — Docker + deploy.** `Dockerfile`, `docker-compose.yml`, `docker compose up` runs the service with `.env`. Deploy to a US-region host with public HTTPS. Checkpoint: `/health` reachable publicly; FMCSA verified live from the deployment. Stop and report.

## 11. Working rules

- One phase at a time. At each checkpoint append to `PROGRESS.md`: what was done, test results, open issues.
- For every non-obvious decision, add to `DECISIONS.md`: problem → choice → rejected alternative → why. Plain English, 3–5 lines each.
- Prefer boring, readable code over clever code. Small functions, type hints, docstrings that explain *why*.
- After each phase, write a short plain-language walkthrough of the new modules in `docs/walkthrough.md` (what each file does, in the order a request flows through it), so the human can explain the code out loud.
- Do not call real `LOAD_BOOK` outside a deliberate, human-approved manual test.
- Never print, log or commit secrets.
