# Deploying carrier-desk

**This service must run in a US region.** FMCSA returns 403 to the whole domain from
outside the United States, so anywhere else the carrier check fails closed and every call
gets routed to a human. Deploying here is also what finally verifies the FMCSA parser —
see `PROGRESS.md`.

What was verified locally, and what was not:

| | Status |
|---|---|
| `docker build` succeeds | verified |
| `docker compose up` serves a full call (verify → OTP → search → negotiate → book → handoff) | verified |
| The container runs as a non-root user | verified |
| `.env` is not baked into the image | verified |
| `PORT` is honoured, healthcheck follows it | verified |
| `ENV=production` + `TMS_MODE=fake` refuses to start (exit 1) | verified |
| **An actual deploy to a US host** | **done** — Render free tier, Ohio, `ENV=development` + `TMS_MODE=fake` |
| FMCSA answering a real response | verified — see "After the first deploy" |
| `ENV=production` + `TMS_MODE=live` against the real tools | **not done** — the deploy above is a development instance |

Everything below the "one command" line was a runbook; it has since been run against the
Render deployment, which is where the FMCSA findings in `PROGRESS.md` come from. The
Railway path remains the recommendation for anything with a phone line attached to it.

## One command

Railway is the recommendation, because the legacy TMS already lives on Railway
(`tramway.proxy.rlwy.net`), so the network path between them is short and stays inside one
provider.

```bash
railway login      # once
railway init       # once, creates the project
railway up         # ship — this is the command, and it is the same one every time
```

Railway detects the `Dockerfile` and builds it. On Fly.io the equivalent is
`flyctl launch --region iad --now` once, then `flyctl deploy` to ship.

## One replica

`docker-compose.yml` pins `deploy.replicas: 1`, and the same applies on any host: **do not
scale this service up yet.**

Run sessions, the negotiation round counters, the negotiation budget, the OTP rate limiter
and the handoff queue are all process-local. A second replica would hold half the state, so
a tool call that landed on the wrong instance would look like a call that had never
started: the carrier would be asked to verify again, or told they had been shown no loads.
Redis behind `RunSessionStore` and `SlidingWindowLimiter` is the path to more than one.
See `DECISIONS.md` §28.

Railway defaults to one replica per service, so this is the default rather than something
to configure — but it is a correctness requirement, not a preference, and it belongs in the
runbook so nobody "fixes" it during a traffic spike.

## Does the free plan sleep the service?

**This is the question that decides the host, and the answer rules out the obvious free
option.**

- **Railway does not sleep idle services.** They run and bill continuously, or they are
  deleted; there is no idle spin-down. The catch is the credit: the trial gives $5 once,
  and the Free plan's recurring credit is about $1/month — roughly 36 hours of a full vCPU.
  A service that never sleeps burns through that quickly, so $5/month Hobby is the
  practical minimum.
- **Render's free web tier does sleep: it spins down after 15 minutes without inbound
  traffic**, and the next request pays a cold start of roughly 20–60 seconds. Browsers get
  a `503` loading page with `Retry-After: 5`.

Fifteen minutes of sleep is fatal for a voice agent. A carrier calls, the first tool call
hangs through a cold start, and the call is over before the service has finished booting —
the failure looks like a broken integration rather than a sleeping host. **So: Railway (no
sleep, continuous billing), not Render free.** The usual workaround for Render — a cron
pinging every 14 minutes — is not something to build a phone system on.

Sources: [Railway pricing](https://railway.com/pricing.md),
[Railway vs Render free tier comparison](https://agentdeals.dev/railway-vs-render),
[what the free tiers actually do](https://dev.to/jarynagent/i-deployed-the-same-api-to-railway-render-and-flyio-free-tiers-one-sleeps-one-throttles-one-3f33),
[Render: Deploy for Free](https://render.com/docs/free).

## Variables to set on the host

The platform's environment, not a `.env` file. `ENV=production` is what arms the startup
guard.

| Variable | Value |
|---|---|
| `ENV` | `production` |
| `TMS_MODE` | `live` (**required** — `fake` refuses to start with `ENV=production`) |
| `TMS_HOST`, `TMS_PORT`, `TMS_TOKEN` | from the challenge |
| `FMCSA_BASE_URL`, `FMCSA_KEY` | from the challenge |
| `OTP_HMAC_SECRET` | generate a fresh one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SERVICE_API_KEY` | generate a fresh one; the HappyRobot workflow sends it as `X-API-Key` |
| `EVENTS_JSONL_PATH` | `/data/events.jsonl`, with a volume mounted at `/data` |
| `REQUEST_BUDGET_SECONDS` | `8` unless the platform's webhook timeout turns out to be tighter |
| `PORT` | set by the platform; the image reads it |

Do not reuse the secrets from the local `.env`. A value that has lived in a developer's
shell should not become a production credential.

## After the first deploy

```bash
curl -s https://<your-host>/health
# {"status":"ok"}

curl -s -X POST https://<your-host>/carriers/verify \
  -H "X-API-Key: $SERVICE_API_KEY" -H "Content-Type: application/json" \
  -d '{"run_id":"deploy-check","mc_number":"123456"}'
```

The second call is the one that matters. In a US region it should return
`"eligible": true` with a real `legal_name` — **that is the first time the FMCSA parser has
been tested against a real response**, and it is what closes open issue #3 in
`PROGRESS.md`. If it returns `fmcsa_rejected:incomplete_response`, the response came back
but the field names in `app/fmcsa/client.py` are wrong; capture the real body, replace
`tests/fixtures/fmcsa_*.json` with it, and delete the `TODO: verify` markers.

The body does not have to be fetched by hand. That same call writes a warning line whose
tail is the carrier record exactly as FMCSA sent it — field names **and** values, capped at
2000 characters, with the web key redacted if the body ever echoed it:

```
WARNING app.fmcsa.client: FMCSA payload for 123456 did not carry the fields we need
(allowedToOperate/outOfService/statusCode). TODO: verify key names.
top_level=['allowedToOperate', 'dbaName', 'dockets', 'dotNumber', 'legalName', ...]
payload={"allowedToOperate": "Y", "legalName": "B MARRON LOGISTICS LLC", ...}
```

Read it out of the host's logs (Render: the service's **Logs** tab). `top_level` is printed
separately so the key names survive even when the body is truncated, and the whole line is
one `grep` away:

```bash
# on the host, or paste the line into an issue
grep "did not carry the fields we need" <service log>
```

Then point the HappyRobot workflow at `https://<your-host>` and set the API key in the tool
configuration.

## What is still fragile in production

- **A restart loses every in-flight call.** Sessions are in memory; a deploy or a crash
  mid-conversation drops the carrier's state and they have to start over.
- **The handoff queue is in memory too.** The `handoff_created` event is the durable
  record — the JSONL log is what you read after a restart, not the queue.
- **One process means one point of failure.** Acceptable for the POC, not for a phone line
  with traffic on it.
