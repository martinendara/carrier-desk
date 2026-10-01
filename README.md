# carrier-desk

[![tests](https://github.com/martinendara/carrier-desk/actions/workflows/tests.yml/badge.svg)](https://github.com/martinendara/carrier-desk/actions/workflows/tests.yml)

Backend service for the inbound carrier sales voice agent (HappyRobot FDE challenge).

A freight brokerage's voice agent handles the first leg of an inbound carrier call:
verify the carrier with FMCSA, send an OTP, pitch a matching load, negotiate the rate
(max three rounds), book it, and hand off to a senior rep.

**This repository is the service the workflow calls as HTTP tools.** It owns all the
business logic and all the integrations. The voice agent only talks and relays.

```
Carrier (web call)
   │
HappyRobot workflow (voice agent, SMS, Twin, Apps)
   │  HTTPS + X-API-Key + run_id
carrier-desk (this repo, FastAPI)
   ├── Legacy TMS  — raw TCP, line protocol, faulty
   └── FMCSA API   — carrier authority
```

The two rules the whole design rests on:

- **The agent never sees `MAX_BUY`.** No response, no spoken line and no log line that
  reaches the agent contains it. Negotiation happens server-side, so the agent cannot
  leak what it does not have.
- **The agent never decides security outcomes.** OTP verification and booking happen
  server-side, and booking uses the rate stored in the session — never a rate the
  request supplied.

## Quickstart

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

cp .env.example .env          # then fill in TMS_TOKEN, FMCSA_KEY, OTP_HMAC_SECRET, ...
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"   # an OTP secret

.venv/bin/python -m pytest
uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000
```

There is no module-level `app`. The factory is deliberate: building the service touches
configuration that must fail loudly when it is wrong, and doing that at import would make
the module unimportable in exactly the case where you want to read the error.

For a local run without a TMS, set `TMS_MODE=fake` (see below).

## Authentication

Every endpoint except `/health` requires the key the platform sends as `X-API-Key`.
Set it with `SERVICE_API_KEY`. If it is unset, the service refuses every request rather
than running open.

## The `run_id` contract

Every request body carries a `run_id`, which the platform injects as a fixed parameter
into all tools. **The agent never sends an `otp_session_id`, a `negotiation_id` or a round
number** — the server resolves all of that from the `run_id`. Request models use
`extra="forbid"`, so sending one is a 422 rather than a silently ignored field.

The server keeps, per `run_id`: the OTP state, the loads shown during that call, the
active negotiation and the round counter. Two consequences:

- A `load_id` can only be negotiated if it was **shown during that same call**.
- A call gets **two negotiations**: the original load plus one alternative.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. No auth. |
| POST | `/carriers/verify` | `{run_id, mc_number}` → eligibility |
| POST | `/otp/send` | `{run_id, mc_number}` → `{masked_destination, delivery}` |
| POST | `/otp/verify` | `{run_id, code}` → `{verified, attempts_left}` |
| POST | `/loads/search` | `{run_id, filters...}` → up to 3 loads, no `MAX_BUY` |
| GET | `/loads/{load_id}?run_id=...` | detail for the pitch, no `MAX_BUY` |
| POST | `/negotiations` | `{run_id, load_id}` → `{offer}` |
| POST | `/negotiations/counter` | `{run_id, carrier_ask}` → `{action, amount}` |
| POST | `/bookings` | `{run_id}` → books at the rate stored in the session |
| POST | `/handoff` | `{run_id}` → mocked senior-rep queue |

Search, negotiation, booking and load detail require a verified OTP for the call.
`/carriers/verify`, `/otp/*` and `/handoff` do not — the handoff is the escape hatch for
a carrier who can never verify, so gating it would turn "route this to a human" into a
dead end.

Interactive docs are at `/docs` once the service is running.

### Result codes

A closed set, so a typo cannot silently never match:

`booked`, `booking_pending`, `negotiation_failed`, `no_match`, `otp_failed`,
`routed_onboarding`, `fmcsa_rejected:<reason>`.

### Errors

Every failure returns `{error, say}`: a machine code and one short sentence the agent can
read out loud. Internal detail goes to the log and never to the response.

```json
{ "error": "not_verified", "say": "Before I can look at loads, I need to confirm the code I sent you." }
```

## A full call, with curl

```bash
BASE=http://localhost:8000
KEY=your-service-api-key
RUN=call-$(date +%s)                  # the platform supplies this; it is fixed per call
H=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")

# 0. Liveness (no auth)
curl -s $BASE/health
# {"status":"ok"}

# 1. Is this carrier allowed to haul for us?
curl -s "${H[@]}" -X POST $BASE/carriers/verify \
  -d "{\"run_id\":\"$RUN\",\"mc_number\":\"123456\"}"
# {"eligible":true,"result_code":null,"legal_name":"RIDGELINE FREIGHT LLC",
#  "dot_number":"3456789","reason":null,"say":"Thanks, I've found your carrier record."}
#
# From outside the US this returns the fail-closed answer instead, because FMCSA
# blocks the whole domain with a 403:
# {"eligible":false,"result_code":"fmcsa_rejected:verification_unavailable",
#  "reason":"verification_unavailable",
#  "say":"I can't verify your authority right now. Let me have someone call you back."}

# 2. Send the code to the contact on file.
curl -s "${H[@]}" -X POST $BASE/otp/send \
  -d "{\"run_id\":\"$RUN\",\"mc_number\":\"123456\"}"
# {"status":"sent",
#  "masked_destination":"d***@ridgelinefreight.com",   <- agent-facing, safe to speak
#  "delivery":{"destination":"dispatch@ridgelinefreight.com","code":"481920"},
#                                                       ^ platform-only, hidden field
#  "result_code":null,"say":"I've sent a six digit code to the email we have on file, d***@ridgelinefreight.com."}

# 3. The carrier reads it back.
#    In the workflow the code never reaches the agent: map `delivery` into a hidden
#    Tool Call Result field and read it from the following node.
curl -s "${H[@]}" -X POST $BASE/otp/verify \
  -d "{\"run_id\":\"$RUN\",\"code\":\"481920\"}"
# {"verified":true,"attempts_left":3,"result_code":null,"say":"Thanks, that's confirmed."}

# 4. Search. At least one filter is required; the cap is three.
curl -s "${H[@]}" -X POST $BASE/loads/search \
  -d "{\"run_id\":\"$RUN\",\"equipment\":\"DRY_VAN\"}"
# {"loads":[{"load_id":"LD00859","orig_city":"Rochester","rate":5126, ...}, ...],
#  "count":3,"result_code":null,"say":"I've got 3 loads that could work."}

# 5. Detail for the one being pitched.
curl -s "${H[@]}" "$BASE/loads/LD00859?run_id=$RUN"
# {"load":{"load_id":"LD00859","commodity":"General freight", ...},"say":"..."}

# 6. Open the negotiation. The load is fetched server-side, so MAX_BUY exists only
#    inside this process.
curl -s "${H[@]}" -X POST $BASE/negotiations \
  -d "{\"run_id\":\"$RUN\",\"load_id\":\"LD00859\"}"
# {"load_id":"LD00859","offer":5125,"say":"I can offer 5125 dollars on that load."}

# 7. The carrier asks for more. One call, one round — the server counts them.
#    These numbers come from the fake, whose MAX_BUY is derived rather than
#    recorded; the shape is what matters here.
curl -s "${H[@]}" -X POST $BASE/negotiations/counter \
  -d "{\"run_id\":\"$RUN\",\"carrier_ask\":5600}"
# {"action":"counter","amount":5525,"say":"I can go up to 5525 dollars."}

curl -s "${H[@]}" -X POST $BASE/negotiations/counter \
  -d "{\"run_id\":\"$RUN\",\"carrier_ask\":5525}"
# {"action":"accept","amount":5525,"say":"Great, we're agreed at 5525 dollars."}

# 8. Book it. No rate in the request: it comes from the session.
curl -s "${H[@]}" -X POST $BASE/bookings -d "{\"run_id\":\"$RUN\"}"
# {"status":"booked","booking_ref":"BR-20261001-0001","result_code":"booked",
#  "say":"You're all set — that load is booked."}

# 9. Or hand the call to a human.
curl -s "${H[@]}" -X POST $BASE/handoff \
  -d "{\"run_id\":\"$RUN\",\"reason\":\"requested\"}"
# {"ticket_id":"rep_9f2c1a4b8d6e","say":"I've asked a rep to pick this up. They'll be with you shortly."}
```

A booking whose response is lost in transport is **never** reported as `booked`. The
client asks the TMS what the load's status is now, and if it cannot be sure the response
is:

```json
{ "status": "booked_unconfirmed", "booking_ref": null,
  "result_code": "booking_pending",
  "say": "I've put a hold on that load. A rep will confirm the details shortly." }
```

## Developing against the fake TMS

Platform workflow tests fire real webhooks, so a development environment needs to run a
whole call without spending live loads. Set:

```
TMS_MODE=fake
```

The service starts the TCP server from `app/tms/fake_server.py` in-process on a loopback
port, seeded from the recorded fixtures in `tests/fixtures/tms_samples.txt`. The real
client talks to it over a real socket, so the deadline, framing, retry and
ambiguous-booking code all still run — only the data is canned.

Loads only captured as `LOAD_QUERY` lines get a synthesised `LOAD_GET` record (including a
`MAX_BUY` above the posted rate) so the flow works end to end. Those invented fields are
marked in the module; nothing about them claims to match the live server.

`TMS_MODE` defaults to `live`.

## Tests

```bash
.venv/bin/python -m pytest          # 395 tests, ~6s
```

Every `LOAD_BOOK` in the suite goes to the fake server. The live server is never
contacted: each successful booking there permanently consumes a load, so a suite aimed at
the real host would destroy the demo data on its first run.

The only thing that touches the live TMS is `scripts/tms_smoke.py`, which refuses any
command outside `DEBUG_ECHO`, `LOAD_QUERY` and `LOAD_GET`.

## Configuration

Everything comes from the environment. `.env.example` documents every variable; `.env` is
gitignored and never committed.

| Variable | Purpose |
|---|---|
| `TMS_MODE` | `live` or `fake` |
| `TMS_HOST`, `TMS_PORT`, `TMS_TOKEN` | live TMS connection |
| `TMS_READ_DEADLINE_SECONDS` | per-attempt read deadline (default 5) |
| `FMCSA_BASE_URL`, `FMCSA_KEY` | authority lookup |
| `OTP_HMAC_SECRET` | key for the stored OTP digests. **Required.** The service refuses to issue codes with a secret shorter than 16 bytes. |
| `OTP_TTL_SECONDS`, `OTP_MAX_ATTEMPTS`, `OTP_MAX_RESENDS` | 300 / 3 / 1 |
| `NEGOTIATION_STEPS`, `NEGOTIATION_URGENT_STEPS` | `0.40,0.75` and `0.60,0.90` |
| `NEGOTIATION_MAX_ROUNDS`, `NEGOTIATION_INCREMENT`, `NEGOTIATION_CEILING_MARGIN` | 3 / 25 / 25 |
| `RUN_SESSION_TTL_SECONDS` | how long a call's state is kept (default 3600) |
| `REQUEST_BUDGET_SECONDS` | wall-clock ceiling per HTTP request, retries included (default 8) |
| `EVENTS_JSONL_PATH` | event log location; `memory` keeps it in-process |
| `SERVICE_API_KEY` | the `X-API-Key` the platform sends |
| `OTP_VERIFY_RATE_LIMIT_PER_HOUR` | attempts per MC per hour, across calls (default 5) |
| `ENV` | `production` arms the startup guard that refuses `TMS_MODE=fake` |

## Deployment

The service must run in a **US region** — FMCSA returns 403 to the whole domain from
anywhere else, so the carrier check fails closed and every call is routed to a human.

```bash
railway login && railway init && railway up   # one-time setup, then `railway up` to ship
```

**One replica.** Run sessions, round counters and the OTP limiter are process-local, so a
second replica would keep half the state and make calls look like they never started.

**The free plan that sleeps is the wrong one.** Railway does not spin down idle services;
Render's free web tier sleeps after 15 minutes of no traffic and pays a ~20–60 s cold
start on the next request — fatal for a voice agent, where the call is over before the
service finishes booting.

Full runbook, required variables and post-deploy checks: [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Where to read more

- [`docs/tms-protocol.md`](docs/tms-protocol.md) — the legacy TMS wire format, and where
  the live server disagrees with the manual.
- [`docs/walkthrough.md`](docs/walkthrough.md) — what each module does, in the order a
  request flows through it.
- [`DECISIONS.md`](DECISIONS.md) — every non-obvious choice: problem, choice, rejected
  alternative, why.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — Docker, the one-command deploy, and the free-tier
  sleep question.
- [`PROGRESS.md`](PROGRESS.md) — the checkpoint log, including what is still open.

## Known open items

- **The TMS token is rejected** (`AUTH_FAILED`) from this machine, so no live load has
  ever been fetched and the four protocol questions in `docs/tms-protocol.md` are
  unanswered. See `PROGRESS.md`.
- **FMCSA field names are recorded, not guessed** — from the live response captured on
  2026-10-01 (`tests/fixtures/fmcsa_carrier_recorded.json`). Two of them changed the
  eligibility rule: `outOfService` does not exist (`oosDate` does), and `statusCode` is the
  carrier record rather than the operating authority. The authority date is the one field
  still unknown, so `authority_age_days` is always `None` and every lead lands on `Review`.
- **Sessions and the handoff queue are in-memory and single-instance.** Redis behind the
  same interface is the production path.
- **OTP delivery is not wired to the platform yet.** The service returns the two views in
  separate fields; mapping `delivery` into a hidden Tool Call Result field is the
  platform's step.
