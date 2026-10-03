# Architecture review — carrier-desk

**Read-only audit.** No code changed to produce this document. Every claim below is
anchored to a file and line in this repository, or to a configuration value that was
read rather than assumed.

Written 2026-10-03, against the deployed instance (Render free tier, Ohio,
`ENV=development` + `TMS_MODE=fake`) and against the intended production shape
(`ENV=production` + `TMS_MODE=live`).

---

## 1. Executive summary

The service is **correct for one process and honest about it**. Every piece of state that
makes a call coherent — the OTP challenge, the shown loads, the negotiation ceiling, the
round counter, the booking outcome — lives in a single Python dictionary, and the design
is explicit that scaling to two of them breaks the call rather than degrading it
(`DECISIONS.md` §28, `docs/DEPLOY.md` "One replica").

That is the right trade for a PoC. It becomes the wrong trade the moment the service is
put in front of a real phone line, and the client's volume (~500 loads/week) does **not**
force that day to come soon: one instance has roughly two orders of magnitude of headroom
on throughput. What will break first is not load — it is **restarts**. The free tier spins
down, and a redeploy or a spin-down mid-call loses everything the call had.

Three findings are worth acting on before anything else, and all three are free:

1. **`/health` is a synchronous handler** (`app/main.py:131`), so it runs in the same
   threadpool as the TMS calls. During a TMS stall, 40 stuck searches can starve the
   healthcheck, and the host restarts a service that was merely waiting.
2. **The event log is not actually durable on the current deploy.** `EVENTS_JSONL_PATH`
   resolves to a path inside the container (`app/config.py:123`, `Dockerfile`), and there
   is **no `render.yaml`** and no mounted volume on Render — so the JSONL that
   `docs/DEPLOY.md` calls "the durable record" is on ephemeral disk and dies with the
   instance.
3. **Nothing ever reaps expired sessions or handoff tickets.** `RunSessionStore.purge_expired`
   exists (`app/sessions/store.py:104`) and has no caller in `app/`; `HandoffQueue`
   (`app/handoff/queue.py:47`) only ever appends. Both grow without bound, slowly, and one
   of them holds ceilings.

Details, the full failure matrix and the capacity arithmetic follow.

---

## 2. State inventory

### 2.1 In process memory

| # | What | Where | Lifetime | Survives a restart? |
|---|---|---|---|---|
| 1 | **Run sessions** — MC, FMCSA verdict, OTP challenge (HMAC, destination, attempts, lock), shown load ids (≤10), negotiations (≤2, each with `ceiling`, `opening_offer`, `current_offer`, `round_number`, `agreed_rate`, `gap`, and the counter-replay fields), booking status + ref | `app/sessions/store.py:47`, model at `app/sessions/models.py:141` | TTL `RUN_SESSION_TTL_SECONDS` = 3600 s, refreshed on every `get_or_create` (`store.py:91`) | **No** |
| 2 | **OTP rate-limiter windows** — one `deque` of monotonic timestamps per key (`mc:<n>` or `run:<id>`), 5/hour/MC by default (100 on the current deploy) | `app/rate_limit.py:43` | Sliding window, 3600 s; dict bounded at `MAX_KEYS` = 10 000 (`rate_limit.py:24`) | **No** |
| 3 | **Handoff queue** — every ticket ever created, each with a `summary` that includes negotiations **and their ceilings** (`app/api/routes/bookings.py:113-127`) | `app/handoff/queue.py:47` | Forever. Nothing trims it and nothing reads it in `app/` | **No** |
| 4 | **Twin upload queue** — an **unbounded** `queue.Queue` plus one daemon thread | `app/twin.py:130`, thread at `:147` | Until sent; dropped on `close()` | **No** |
| 5 | **Booking lock** — one coarse global `threading.Lock`, not per run | `app/container.py:112` | Process | **No** |
| 6 | **Counter-replay memory** — last ask/action/amount/time per negotiation, 15 s window | `app/sessions/models.py:127`, logic at `app/negotiation/engine.py:330` | Part of the session; same TTL | **No** |
| 7 | **Fake TMS booked-load set** (`TMS_MODE=fake` only, i.e. the current deploy) | `app/tms/fake_server.py:206` | Process | **No** |
| 8 | Settings singleton | `app/config.py:176` (`lru_cache`) | Process | Re-read from env |

Two of these are **unbounded**, which is a separate point from being volatile:

- Sessions expire lazily — an entry is only removed when it is read after its deadline
  (`store.py:74`) or when someone calls `purge_expired`. **Nobody calls it.** At 500
  calls/week and ~2 KB per session, that is tens of MB per year, so it is not urgent, but
  it is growth with no ceiling.
- The handoff queue is a plain list with no cap, holding one dict per handoff
  **including the ceiling**. Same order of magnitude, same absence of a bound.

### 2.2 On local disk

| What | Where | Survives a restart? |
|---|---|---|
| **Event log** (`events.jsonl`) | `app/config.py:123`, default `var/events.jsonl`; the Dockerfile forces `/data/events.jsonl` | **Locally with the compose volume: yes. On the current Render deploy: no.** `docker-compose.yml` declares a named volume; there is no `render.yaml` and no volume on the Render service, so `/data` is the container's own filesystem |
| **Twin backfill checkpoint** | `scripts/twin_backfill.py:24`, `var/twin_backfill_checkpoint`, relative to CWD | No. Losing it re-sends from the beginning — safe, because row ids are content-derived and idempotent (`app/twin.py:51`), but it re-uploads |

The event write itself is deliberately non-fatal: an `OSError` is logged and swallowed so
a full disk cannot fail a live call (`app/events/sink.py:79`). The consequence is that
**event loss is silent by design** — the only signal is a log line.

### 2.3 External dependencies

| Dependency | Protocol | Timeout / retry | Failure direction |
|---|---|---|---|
| Legacy TMS | raw TCP, one connection per request | connect 3 s, read 5 s per attempt, 3 attempts, backoff 0.3/0.8/1.5 s, all clamped by an 8 s per-request budget | Raises; the call is told the load search failed |
| FMCSA | HTTPS | 5 s, one attempt; the authority sub-call is capped at 2 s and swallowed | **Fails closed** — `verification_unavailable`, not eligible (`app/fmcsa/client.py:268`) |
| Resend | HTTPS | 5 s, 2 attempts | `send_failed`; nothing is issued and the resend budget is untouched (`app/otp/service.py:239`) |
| HappyRobot Twin | HTTPS | 3 s, 3 attempts, background thread | Logged and dropped; the call never notices |

---

## 3. Failure matrix

### 3.1 Two instances (horizontal scale)

**This is the failure the architecture is built to avoid, and it does not degrade — it
corrupts the call.**

| Symptom | Why |
|---|---|
| Carrier is asked to verify again, mid-call | The OTP challenge lives on instance A. A tool that lands on B finds no session, `get_or_create` starts a fresh one, `require_verified` raises `NotVerified` → 403 (`app/api/deps.py:42`) |
| A load the carrier was just shown cannot be negotiated | The shown-loads list is on A. On B, `open` raises `LoadNotShown` → 409 (`app/negotiation/engine.py:149`). The carrier hears "that load isn't one I showed you" about a load they *were* shown |
| Round counter resets | Two-negotiation budget and round number are per session. A call split across two instances can be handed the ladder twice |
| OTP guessing budget doubles | The limiter is per process. N instances → N × 5 attempts/hour/MC |
| Handoff tickets split | A rep reading instance B does not see A's queue. (The `handoff_created` event is the record that survives — if it reaches a durable sink, which on the current deploy it does not) |
| Torn JSONL lines | Two processes appending to one file each hold their own lock (`app/events/sink.py:65`) |
| A booking can land as `booking_pending` unnecessarily | The booking lock is per process (`app/container.py:112`), so two instances can both reach `LOAD_BOOK`. The second gets `ALREADY_BOOKED` → `booked_unconfirmed`. **Not a double booking** — the TMS keys bookings per token and monotonic — but a worse answer for a booking that actually succeeded |

**Severity: critical.** **Matters in the PoC: no, and only because one replica is pinned**
(`docker-compose.yml` `deploy.replicas: 1`, `DECISIONS.md` §40, `docs/DEPLOY.md`). The risk
is not the current configuration; it is that scaling up is the instinctive response to a
Monday-morning latency complaint, and the symptom (carriers re-verifying) does not look
like a scaling bug.

### 3.2 Redeploy mid-call

Everything in §2.1 is gone. What the carrier experiences is §3.1 with no recovering
instance: the next tool call 5xx's or 403's, and the conversation has to restart from
verification.

The dangerous window is narrower than "the whole call", and it is worth naming:

| Moment of restart | Consequence |
|---|---|
| Before `LOAD_BOOK` is sent | Clean. The session is gone, the carrier starts over, no load is consumed |
| **Between the `LOAD_BOOK` write and the response** | **The worst case.** The load is consumed at the TMS, the session that held the agreed rate and the booking status is gone, and the platform gets a connection error. `booking_attempted` was already written (`app/api/routes/bookings.py:62`), so the durable log shows an attempt with no matching `booking_result` — a rep can reconcile it with a `LOAD_GET`, but only if the event survived, which on this deploy it does not |
| After `booking_result`, before `handoff` | A booking with no ticket. The `handoff_created` event is the only record |

**Severity: high.** **Matters in the PoC: yes** — the free tier spins down after 15
minutes of inactivity, so a restart is not a rare event, it is the normal state of the
instance between calls. The deploy also loses the event log on every spin-down, which
means the audit trail for the demo depends entirely on Twin being enabled.

### 3.3 Twin down

| Effect | Assessment |
|---|---|
| The call | **Unaffected.** `TwinWriter.emit` hands the event to a queue and returns (`app/twin.py:139`); the send happens on a daemon thread. This is exactly the right shape |
| The event | Still written to JSONL by the wrapped sink **before** it is queued (`app/twin.py:139`), so nothing is lost *if the disk is durable* |
| The queue | **Unbounded.** `queue.Queue()` with no `maxsize`. A long Twin outage with steady traffic grows it without limit. At PoC volume (hundreds of events) this is irrelevant; it is still a memory-growth path with no ceiling |
| Recovery | `scripts/twin_backfill.py` re-uploads from JSONL and is idempotent by content-derived row id (`app/twin.py:51`). It also relies on a **local checkpoint file**, so on an ephemeral disk it restarts from the beginning — safe, just wasteful |

**Severity: low.** **Matters in the PoC: no.** This is the best-handled dependency in the
service: it cannot affect a call, and it has a working recovery path.

### 3.4 TMS slow or down

Two distinct behaviours, and they are worth separating because the mitigations differ.

**Slow** (answering, but past the deadlines):
- The per-request `Deadline` (8 s, `app/config.py:73`) bounds the whole HTTP request, not
  one attempt. A retry whose backoff would not fit is skipped and the failure is reported
  as `TmsBudgetExhausted` → 504 rather than as the underlying timeout (`DECISIONS.md` §34).
  That is the right call: it is the one failure here the platform can usefully retry.
- The carrier hears a sentence (`app/api/phrasing.py`) instead of silence. Correct.

**Down** (no bytes, ever):
- Every search burns the full budget. The tool answers 504 after ~8 s. The call is
  degraded but not corrupted.
- **The real risk is thread exhaustion, and it is worse than it looks.**
  `@app.get("/health")` is declared as `def health()` (`app/main.py:131`), not `async def`,
  so FastAPI runs it in the **same bounded threadpool** as every TMS-touching handler. With
  40 threads (anyio's default) all occupied by 8-second searches, `/health` queues behind
  them. The Docker healthcheck has `--timeout=5s --retries=3`, so a TMS stall can make the
  container report unhealthy and get restarted — **while the service is working exactly as
  designed**. On a platform that restarts unhealthy containers, a TMS outage becomes a
  restart loop, and a restart loses every in-flight call (§3.2).
- Booking is the one command with no blind retry: on a transport fault the client calls
  `LOAD_GET` to find out whether the write landed, and reports `booked_unconfirmed` →
  `booking_pending` when the evidence is inconclusive (`DECISIONS.md` §5, §11).
- **Untested against the live server.** The TMS token has been rejected since 2026-10-01
  (`PROGRESS.md`), so no live load has ever been fetched and none of these paths has run
  against the real host. The fault modes are the manual's, not observations.

**Severity: high** (through the healthcheck interaction), otherwise **medium**.
**Matters in the PoC: partly** — the current deploy runs `TMS_MODE=fake`, where the fake
server answers in milliseconds and never stalls, so this failure mode is invisible today
and will appear for the first time in production.

### 3.5 FMCSA down

- Fails closed by construction: a timeout, a 5xx, a 403, an unreadable body or an
  error status all resolve to not-eligible, with `verification_unavailable` where the cause
  is the call itself failing (`app/fmcsa/client.py:268`, `DECISIONS.md` §27).
- The consequence is an **availability** loss, not a correctness one: every call routes to
  a human, and the carrier is told to expect a callback. Nothing is booked on an
  unverified carrier.
- No caching: every `/carriers/verify` is a fresh FMCSA round trip, plus a second one to
  the per-DOT authority endpoint for carriers that hold an authority (`app/fmcsa/client.py:345`).
  500 calls/week is nothing for FMCSA, so this is not a load problem — it is a
  latency-and-availability one, since an FMCSA outage is felt on the very first tool call
  of every conversation.
- From outside the US the whole domain returns 403, so a non-US deployment fails closed for
  100% of carriers (`README.md`, `docs/DEPLOY.md`).

**Severity: medium.** **Matters in the PoC: yes, visibly** — it is a live dependency of the
first tool of every call, and on the current deploy it is the reason `authority_age_days`
is always `None` and every lead lands on `Review` (`DECISIONS.md` §48).

---

## 4. Capacity — one instance against ~500 loads/week

### 4.1 The offered volume

If "500 loads/week" means 500 conversations (and up to 500 bookings), the arithmetic is
comfortable by a wide margin. The client's peaks are named as Monday morning and Friday
afternoon; assuming both peaks compress the week's work into short windows:

| Measure | Value |
|---|---|
| Calls per week | 500 |
| Average | ~71/day, ~3/hour |
| Monday peak, modelled as 40% of the week inside a 4-hour window | 200 calls / 4 h = **50 calls/hour** |
| Concurrent calls at that peak, at a ~4-minute call | 50 × 4/60 ≈ **3–4 concurrent calls** |
| Same peak if every call lasted 8 minutes (a long negotiation) | ~7 concurrent calls |

### 4.2 What one instance costs per call

Each call makes 6–9 upstream round trips, serialised by the conversation rather than
competing for CPU:

```
verify       → 1 FMCSA HTTPS (+0–1 for the authority record)
send_otp     → 1 Resend HTTPS
verify_otp   → 0 (in-process HMAC)
search       → 1 LOAD_QUERY + up to 3 LOAD_GET   (4 TMS round trips)
open         → 1 LOAD_GET
counter ×1–3 → 0 (pure arithmetic on an in-memory session)
book         → 1 LOAD_BOOK (worst case +1 LOAD_GET +1 LOAD_BOOK)
handoff      → 0
```

Measured behaviour on the deploy is 0.3–0.7 s per tool call against the fake TMS
(`PROGRESS.md`), and ~360 ms for a TMS round trip on the live host. Against the real TMS a
search is therefore ~1.5 s typical, bounded at 8 s worst case by the budget.

**Thread arithmetic.** The binding resource is the 40-thread worker pool that sync handlers
share. Each tool call holds a thread for its duration.

| Scenario | Thread-seconds per call | Calls per minute, 40 threads |
|---|---|---|
| Typical live TMS (~1.5 s/search × ~8 tool calls) | ~12 s | ~200 |
| Every TMS call hits the 8 s budget (outage) | ~64 s | ~37 |

At 50 calls/hour = 0.83 calls/minute at peak, the service is using **well under 1%** of the
thread budget in the typical case and ~2% in the pathological one. Throughput is not the
constraint, and it will not be until the volume is two orders of magnitude larger.

**Memory.** A session is a few KB; 100 concurrent sessions is a rounding error. The rate
limiter is capped at 10 000 keys. The only unbounded structures are the Twin queue and the
handoff ticket list (§2.1), and neither is a throughput problem at this volume.

### 4.3 What actually limits one instance

Not load. In order of what will bite first:

1. **Cold starts.** The free tier sleeps after 15 minutes and pays ~20–60 s to wake. For a
   voice agent that is not latency, it is a failed call — the reason `docs/DEPLOY.md`
   recommends Railway over Render free. This is the single largest capacity constraint in
   the current setup, and it is entirely a hosting-choice artefact.
2. **Restarts.** Any restart loses every in-flight call (§3.2), so the *effective* capacity
   is bounded by how often the process restarts, not by requests per second.
3. **Threadpool starvation during a TMS stall**, via `/health` (§3.4).
4. **No reaper** for sessions and handoff tickets (§2.1), which is growth, not throughput.

If the client's real load ever demanded more than one instance, the answer would still not
be "more workers" — it would be Redis behind the existing interfaces, because that is what
makes the second instance safe.

---

## 5. Risk register

Severity is the impact **if it happens**; "PoC" is whether it can happen in the current
demo. Fixes are free unless marked **[paid]**.

| # | Risk | Sev. | PoC? | Phase-2 fix |
|---|---|---|---|---|
| R1 | Two instances split call state; a call is corrupted, not degraded | Critical | No (1 replica pinned) | Redis (or Postgres) behind `RunSessionStore`, `SlidingWindowLimiter` and `HandoffQueue`. **[paid]** Render Key Value / Upstash, a few $/month; **[free]** Neon/Supabase Postgres tier, or sticky routing by `run_id` as a stopgap — though sticky routing does **not** survive a restart |
| R2 | Restart mid-call loses the session; worst case an orphaned booking with no recorded ref | High | **Yes** — the free tier restarts routinely | Make the session store durable alongside R1. Cheaper interim: treat `booking_attempted` without a `booking_result` as a reconciliation queue for a rep — but this needs the event to survive, i.e. R3 |
| R3 | The event log lives on ephemeral disk, so "the durable record" is not durable | High | **Yes** | **[free]** enable `TWIN_ENABLED` (Twin is already the second sink and is content-idempotent); or **[paid]** mount a Render disk (~$0.25/GB/mo) at `/data` |
| R4 | A TMS stall starves the healthcheck through the shared threadpool → restart loop | High | No (`TMS_MODE=fake` answers instantly) | **`async def health`** — free, one word. Optionally raise the healthcheck tolerance |
| R5 | OTP guessing budget multiplies with instance count | Medium | No | R1's shared limiter. Independent of R1: the per-call lock already caps a single call at 3 |
| R6 | No reaper for expired sessions or handoff tickets; both grow without bound, one holds ceilings | Medium | Yes, but slowly | Call `purge_expired()` from a background task or the healthcheck; cap `HandoffQueue`. Both free |
| R7 | Twin's upload queue is unbounded | Low | No | Bounded queue with an explicit drop policy and a counter, so the drop is visible rather than silent. Free |
| R8 | FMCSA is called on every verify with no cache; an outage is felt on the first tool of every call | Medium | Yes | Short-TTL cache keyed by MC (minutes), tolerated staleness documented. Free. Fail-closed behaviour stays exactly as it is |
| R9 | The live TMS path has never run: token rejected since 2026-10-01 | High | No — it is the reason the deploy runs `fake` | Not a design fix: get a working token and run `scripts/tms_smoke.py`. All the fault handling in §3.4 is untested against the real host |
| R10 | `authority_age_days` is always `None`, so every lead is `Review` — the onboarding rule is currently a constant | Low | Yes | Source a real authority date (`DECISIONS.md` §48). Until then `Review` is the correct, honest answer |
| R11 | A single process is a single point of failure | Medium | Yes | Accepted for the PoC. The fix is R1 + a second replica behind a load balancer |
| R12 | Two processes appending to one JSONL can interleave | Low | No (single instance; and on Render there is no shared volume) | Only relevant once R1 lands. An external sink removes the question |

### 5.1 What I would do in phase 2, in order, if the budget were zero

1. **R4** — `async def health`. One word, removes a restart loop.
2. **R6** — call `purge_expired()` and cap the handoff queue. ~10 lines.
3. **R3** — flip `TWIN_ENABLED`, so the audit trail is off-box before the demo.
4. **R8** — an MC-keyed FMCSA cache with a short TTL.
5. **R1 → R2** — Redis behind the three in-memory interfaces. This is the one that needs
   money and a design pass, and it is what unlocks a second replica.

### 5.2 Paid options, named

Only where a free path does not exist. These are phase-2 items, not PoC items.

- **Redis:** Render Key Value, Upstash or Redis Cloud. A few $/month at POC size. This is
  the canonical answer for R1/R2/R5 and the only one the code was designed for — all three
  stores are already interfaces (`DECISIONS.md` §28).
- **A mounted disk:** Render disks, ~$0.25/GB/month, for R3 if an external sink is
  unacceptable. Note it also pins the service to one instance, which is fine here.
- **A non-sleeping host:** Railway Hobby, ~$5/month, or Fly.io. Not a scale question — a
  call-availability one (`docs/DEPLOY.md`). The current Render free tier is the largest
  single threat to a live demo.

---

## 6. What is already well solved

These are load-bearing and worth stating explicitly, because they are the parts that would
be expensive to get wrong.

**1. The ceiling cannot reach the agent — enforced at the data layer, not at call sites.**
`LoadDetail.max_buy` is `repr=False` and `public_dict()` strips it, along with any unknown
field spelled `MAX_BUY` in any case (`app/tms/models.py`, `DECISIONS.md` §9). `Load.rate`
is stripped the same way (§52), so the posted rate cannot be quoted either. Because the
stripping happens where the field is defined rather than in each serializer, a new endpoint
is safe by default. `tests/test_api.py::test_no_load_response_ever_contains_max_buy` and
`test_a_full_call_never_puts_the_ceiling_in_front_of_the_agent` pin it.

**2. Negotiation decisions are made server-side, and the agent cannot supply a number that
matters.** `counter_offer` answers from the session's `ceiling`, `current_offer` and
`round_number`, none of which appear in any request (`app/negotiation/engine.py:208`). The
round number is counted server-side, so it cannot be skipped, replayed or restarted. On
`close` the response carries the *last offer*, never the ceiling — the tempting value at
the one moment it must not leak (`DECISIONS.md` §21). `CLARIFY` catches a misheard number
without spending a round (`engine.py:228`).

**3. Booking cannot be double-driven and cannot be driven with a substituted rate.**
`POST /bookings` takes only `run_id`; the rate comes from `session.agreed_negotiation`
(`app/api/routes/bookings.py:53`). The outcome is stored on the session and the whole
check-and-book is serialised by a lock, so a platform retry — and the platform's minimum is
one retry — returns the stored answer instead of a second `LOAD_BOOK` (`bookings.py:47`,
`DECISIONS.md` §57). An `agreed_rate` in the body is a 422.

**4. A counter retry replays instead of consuming a round.** A repeat of the same ask
inside 15 s returns the stored decision, checked *before* the open/closed gate so an
accept or a close also replays (`app/negotiation/engine.py:330`, `DECISIONS.md` §50). The
15 s window is what distinguishes a webhook retry from a carrier genuinely repeating their
number.

**5. The TMS retry policy is asymmetric on purpose, and the asymmetry is correct.**
`LOAD_QUERY`/`LOAD_GET` retry three times with jittered backoff on transport faults but
never on `AUTH_FAILED`, `MISSING_FIELD`, `UNKNOWN_LOAD`, `UNKNOWN_CMD`, `ALREADY_BOOKED`,
`INVALID_RATE` or `MALFORMED` — those are answers, not accidents. Unknown codes *are*
retried, because an undocumented failure is more likely transient than a request we got
wrong (`app/tms/client.py:98`, `DECISIONS.md` §4).

**6. `LOAD_BOOK` is never retried blindly.** On a transport fault the client asks the TMS
what happened: still `OPEN` → retry once; no longer `OPEN` → booked by us →
`booked_unconfirmed` with a null ref for a rep to confirm; a second ambiguous outcome →
stop (`app/tms/client.py:227`, `DECISIONS.md` §5, §11). The alternative — a blind retry —
risks a double booking, and inventing a reference is worse than admitting uncertainty.

**7. A truncated TMS response is never presented as a short result.** A response without
`END` is `TmsPartialResponse`, including the unterminated trailing `END` the live server
actually sends — which also means a complete response is not charged the full deadline
(`DECISIONS.md` §7, §8). Rule 4 of the brief holds with no heuristic.

**8. The verification gate cannot be forgotten.** `require_verified` is called explicitly by
each gated handler, and the protection against omission is a test that walks the app's own
route table: every route not on the short "works before verification" list must answer 403
to an unverified call, and any route missing from the test's body map fails it
(`app/api/deps.py:32`, `DECISIONS.md` §29). A new endpoint is gated by default. Load detail
is gated too, though §8 does not list it, because pitching freight to an unverified caller
is what verification exists to prevent (§30).

**9. The agent cannot address session state or name a carrier to verify against.**
Everything is resolved from the platform-injected `run_id`; request models set
`extra="forbid"`, so sending an `otp_session_id`, a `negotiation_id`, a round number or an
`mc_number` to `send_otp` is a 422 rather than a silently ignored field
(`app/api/schemas.py`, `DECISIONS.md` §12, §51). `send_otp` sends to the MC that
`verify_carrier` recorded on the session, and the OTP destination comes only from
`carriers/directory.py` — never from the caller (§15, `app/otp/service.py:183`).

**10. OTP storage is keyed, not merely hashed.** HMAC-SHA256 with a server secret and the
`run_id` mixed into the message, so a stolen table is worthless and a digest captured in one
call cannot be replayed in another; the service refuses a secret shorter than 16 bytes
(`app/otp/security.py`, `DECISIONS.md` §16). A resend does not refund attempts and a locked
session cannot be resent (§17), and the limiter counts *every* attempt including a correct
one (`app/rate_limit.py`, §38).

**11. A load can only be negotiated if it was shown during that call.** `open` refuses
anything else with `LoadNotShown`, and fetching a detail deliberately does **not** count as
showing it — otherwise "negotiate what you were shown" collapses into "negotiate anything
you can name", and load ids are short and guessable (`app/negotiation/engine.py:149`,
`DECISIONS.md` §13, §31).

**12. Failures speak to the carrier and log to the operator, from one place.** Every failure
is a closed-set machine code plus one sentence from `app/api/phrasing.py`; the exception
type and message go to the log. A generic handler catches anything unmapped so the response
shape holds even for a bug (`app/api/errors.py`, `DECISIONS.md` §35). Request validation
errors log the offending *field and reason* and deliberately drop the value that failed
(`errors.py:_validation_error`).

**13. A misconfiguration refuses to start rather than starting and failing.** `ENV=production`
refuses a fake TMS, an empty API key, an empty OTP secret or an empty Resend key, in that
order; a live TMS with a missing `TMS_HOST`/`TMS_PORT`/`TMS_TOKEN` names the variable
instead of crash-looping (`app/container.py:36`, `DECISIONS.md` §39, §42, §49). A deployment
that starts and cannot work gets debugged at 2am against a live carrier.

**14. The event log is a separate store from the conversation, and is allowed to hold the
ceiling.** `{run_id, ts, type, payload}` with in-memory and JSONL sinks, and the shape the
Twin adapter plugs into (`app/events/sink.py`, `DECISIONS.md` §26). The ceiling in an event
is safe because no event ever travels back to the agent, and the Twin row id is derived
from content so retries and backfills are idempotent (`app/twin.py:51`).

**15. Money is computed with exact rationals.** The concession steps are `Fraction`s, not
floats, because 0.6 is stored as 0.5999... and the product with the gap can land a hair
below a multiple of 25, where `floor25` would silently drop a whole increment
(`app/negotiation/policy.py`, `DECISIONS.md` §23). This decides what the brokerage pays.

---

## 7. Method and limits

- **Read, not run.** No code was modified and no load test was executed. The capacity
  arithmetic in §4 is derived from the documented timeouts and the observed per-call
  latency in `PROGRESS.md`; it is an order-of-magnitude estimate, not a benchmark. The
  single most valuable follow-up would be a load test against a `TMS_MODE=fake` instance
  with a deliberately slowed fake server, which would turn §4.2 into measurements.
- **The live path is untested.** The TMS token has been rejected since 2026-10-01, so every
  statement about live-TMS behaviour comes from the manual and the client's own code, not
  from observation. Any capacity figure involving the real host is a prediction.
- **What was verified directly:** the configuration defaults (`app/config.py`), the
  presence or absence of callers for `purge_expired`, the absence of a `render.yaml`, and
  the handler signatures in `app/main.py`.
