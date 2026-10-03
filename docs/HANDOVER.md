# HANDOVER — carrier-desk

For a fresh session to resume without re-deriving anything. Read this, then
`git log --oneline -3`.

**Frozen as of 2026-10-03.** The build is complete, published and verified. No further
code changes are planned; treat everything below as the end state.

## Repo state

- Branch `main`. Tests: **678 passed**; GitHub Actions **green**.
- **Deployed and live: `237a314`** on `https://carrier-desk.onrender.com` —
  `237a314bd2362d731e838ca146c73b175ee7cfc0`, read from `/health` after the deploy went
  Live. `ENV=development`, `TMS_MODE=fake`, `TMS_FAKE_FAULT_RATE=0.10`,
  `OTP_VERIFY_RATE_LIMIT_PER_HOUR=100`.
  *Documentation commits move this hash without changing behaviour. The last commit that
  touched code was `a624efc`; everything after it is docs and tests.*
- `python -m pytest` is the whole suite; it needs no `.env` (fake TMS + mocked FMCSA).
- **The deploy's `X-API-Key` is the value in `.env` named `RENDER_API_KEY`.** The name is
  misleading: that key does **not** work against `api.render.com` (it answers 401 there). It
  is the service's own key, and it is what `scripts/qa_run.py` wants as `QA_API_KEY`.

## Final state (2026-10-03)

| | |
|---|---|
| Service | Deployed on Render, **Live on `237a314`**, verified from `/health` |
| Voice workflow | **Published**, running the prompt in `docs/prompt-live.md` verbatim |
| Adversarial tests (platform) | **6/6** — the 10:07 run, after two rounds of corrections |
| App | **v2 published** — built against `docs/APP_SPEC_TANDA2.md` |
| Tests | **678 passed**, three consecutive runs |
| Twin | `twin/schema.sql` pasted, all views live |

### The adversarial runs, in order

| Time | Score | What it was |
|---|---|---|
| 8:21 | 5/6 | **Invalid.** `@mc_number` stayed plain text in the actor prompts, so no test got past asking for the MC. The score measured the harness, not the agent |
| 9:08 | 3/6 | Real. The close hinted at a ceiling and promised a human follow-up; the pitch read out too many numbers |
| 9:30 | — | Ceiling and style corrected. Surfaced a new failure: the offer repeated when the carrier gave no amount |
| 10:07 | 6/6 | |

**The 8:21 result is recorded here because it is the trap.** A green-adjacent score from a
harness that never reached the behaviour under test reads exactly like a near-miss, and it
sent the first round of work at two defects nobody had actually observed on a call. Before
trusting a failing run, check that the run reached the thing it was scoring.

### The corrections

The first two were made before the harness was found broken — they are fixes to defects read
in the prompt and a transcript, not to anything a scored run demonstrated. The last three
came from the runs above.

1. **The close ended the call twice.** The close `say` does not sound like a goodbye, so
   `Ending` took its "otherwise, say one short goodbye" branch and added a second farewell;
   and the "ask for a person" rule, unbounded, fired a handoff after the close it was
   sitting next to. A defined ending is now terminal whatever the `say` sounds like, and the
   person rule is bounded to before the call ends.
2. **The agent invented a six-digit MC.** The claim was in the transcription context
   ("MC numbers (6 to 7 digits)"), not in the prompt. Fixed there, and the prompt's two
   six-digit examples became a neutral five-digit one. The service never required six: its
   validation is `^\d{1,8}$`, so `15735` always passed.
3. **The close revealed a ceiling and promised a rep** (9:08). "I can't go higher than that"
   confirms an upper bound exists without naming it, and nobody follows up on a walk-away.
   Rewritten, and every other agent-facing string audited for the same two faults.
4. **The pitch read out numbers to someone driving** (9:08). It now leads with route, pickup
   and equipment, then the offer; weight, miles and dimensions are answered only if asked.
5. **A carrier who pushed without naming a price heard the offer repeated** (9:30) until the
   call ran out of turns. The agent now asks once, converts a relative figure to a total,
   and knows that asking costs no round.

### The Northstar criterion was a mis-transcription

The tone rule settled at design time was **one price per turn** (DECISIONES §5 — the design
document; this repository's §5 is about `LOAD_BOOK`). The platform's "Brief and
conversational" criterion was written as **one number per turn**, which is a different rule
and a worse one.

A load offer carries a route, a day and equipment alongside the price — that is what an
offer is, not a violation of it. The old wording made the normal, correct pitch look like the
thing being scored, while the rule it was meant to carry is about prices: the carrier's
number confirmed on its own, the offer spoken on its own. Correcting the criterion aligns the
judge with the rule the design already had.

The pitch was trimmed in the same pass — weight and miles only if the carrier asks — so the
turn that had genuinely been crowded got a real fix rather than only a re-worded test.
