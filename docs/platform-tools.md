# Platform tools — HappyRobot workflow reference

One card per agent tool, in call order. `verify_carrier` already exists in the
workflow and is the model these follow (see `docs/workflow-sample.json`). Build
each of the rest the same way: a **Tool** node the agent calls, with a **Webhook**
child node that does the HTTP call.

## Conventions (identical for every tool)

**Chips.**

- `{{SERVICE_BASE_URL}}` — use-case variable, e.g. `https://carrier-desk.onrender.com`.
- `{{SERVICE_API_KEY}}` — use-case variable; sent as the `X-API-Key` header.
- `{{run_id}}` — **Fixed value**, bound to the platform's Run ID chip
  (`group: current`, `variable: run_id`). The agent never sees or sets it.
- `{{$var:<tool>.<param>}}` — a value from the tool node's own parameters; the
  child-node body references it by the tool node's persistent id, exactly as
  `verify_carrier`'s body does.

**Child node (Webhook) — shared.** Every tool's child node is a Webhook action
with `authType: none`, `contentType: application/json`, `webhookSchemaVersion: 2`,
and the header `X-API-Key: {{SERVICE_API_KEY}}`. Only the method, URL/params and
body differ.

**`run_id`.** Always the only **Fixed value** parameter, bound to the Run ID
chip. Every other parameter is **Agent decides** (filled from the conversation).

**Response audience.** `say` is always agent-facing — it is the sentence the
agent relays, in its own words. `result_code` (where present) is the closed-set
code the *platform* branches on; the agent never reads it out. `delivery` on
`send_otp` is the one field that is never agent-facing.

**Blocking.** Every tool is blocking: the agent waits for the response before it
speaks.

---

## 1. `send_otp` — POST `/otp/send`

**Description (agent).** Send a one-time code to the email on file for the
verified carrier. Call right after `verify_carrier` returns `eligible: true`.
Do not call before verifying, and do not call again to "retry" a code the caller
did not receive — a second call is a resend, and there is only one.

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |

No `mc_number`: the server sends to whatever carrier `verify_carrier` recorded
for this `run_id`. There is nothing for the agent to name.

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/otp/send`
- Headers: `X-API-Key: {{SERVICE_API_KEY}}`, `Content-Type: application/json`
- Body:
```json
{
  "run_id": "{{$var:<tool>.run_id}}"
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `status` | Agent | `sent` \| `routed_onboarding` \| `resend_limit_reached` \| `already_verified` \| `locked` (also when no carrier was verified) \| `send_failed` |
| `masked_destination` | Agent | e.g. `m***@gmail.com` — safe to say |
| `result_code` | Agent (routing) | `null` \| `routed_onboarding` \| `otp_failed` |
| `say` | Agent | speak this |

No `delivery` field: the service sends the email itself, so the plaintext code
never transits the platform.

**Retries / blocking / end call.** Retries: 1 (idempotent — a retry after a lost
response issues a fresh code, which spends the one resend; acceptable, the
server caps it). Blocking: yes. End call: no.

**Additional node.** None — the email is sent server-side (Resend).

---

## 2. `verify_otp` — POST `/otp/verify`

**Description (agent).** Check the 6-digit code the carrier read back. Call once
the caller gives the code, after `send_otp`. Do not call before sending, and do
not re-check a code that already verified.

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |
| `code` | Agent decides | string | `483920` | yes |

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/otp/verify`
- Body:
```json
{
  "run_id": "{{$var:<tool>.run_id}}",
  "code": "{{$var:<tool>.code}}"
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `verified` | Agent | bool — branch on it |
| `attempts_left` | Agent | for `say` ("…you have N tries left") |
| `result_code` | Agent (routing) | `null` \| `otp_failed` |
| `say` | Agent | speak this |

**Retries / blocking / end call.** Retries: 1 (idempotent — a replayed verify on
an already-verified session returns `verified: true`, not a new attempt).
Blocking: yes. End call: no.

**Additional node.** None.

---

## 3. `search_loads` — POST `/loads/search`

**Description (agent).** Search matching loads by origin, destination, equipment
or pickup date. Call only after the OTP is verified. Do not call before
verification, and do not invent filters the carrier did not give. On no matches,
take the caller's details and end the call — do not hand off.

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |
| `orig_city` | Agent decides | string | `Chicago` | no |
| `orig_state` | Agent decides | string | `IL` | no |
| `orig_zip` | Agent decides | string | `60607` | no |
| `dest_city` | Agent decides | string | `Columbus` | no |
| `dest_state` | Agent decides | string | `OH` | no |
| `dest_zip` | Agent decides | string | `43215` | no |
| `equipment` | Agent decides | string | `DRY_VAN` | no |
| `pickup_date` | Agent decides | string | `20261003` | no |

At least one filter is required. Only the fields the carrier gave go in the body.

`equipment` values are short upper-case identifiers. `DRY_VAN` is the one
confirmed in the recorded data; `REEFER` / `FLATBED` are candidates, not
confirmed. An unknown value returns no matches (no error).

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/loads/search`
- Body (send only the filters that are filled):
```json
{
  "run_id": "{{$var:<tool>.run_id}}",
  "orig_state": "{{$var:<tool>.orig_state}}",
  "equipment": "{{$var:<tool>.equipment}}"
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `loads` | Agent | list of up to 3 load objects (see below); **never contains `MAX_BUY`** |
| `count` | Agent | 0–3 |
| `result_code` | Agent (routing) | `null` \| `no_match` |
| `say` | Agent | speak this |

Load object fields (agent-visible): `load_id`, `orig_city`, `orig_state`,
`orig_zip`, `dest_city`, `dest_state`, `dest_zip`, `pickup_dt`, `eqtype`,
`miles`, `status`, plus the full detail — `delivery_dt`, `weight`, `commodity`,
`pieces`, `dims`, `notes`. No `rate`, no `max_buy`: the price comes only from
`open_negotiation`.

**Retries / blocking / end call.** Retries: 1 (idempotent read). Blocking: yes.
End call: no.

**Additional node.** None.

---

## 4. `open_negotiation` — POST `/negotiations`

**Description (agent).** Open a negotiation on a load shown this call and get the
opening offer. Call when the carrier wants to talk price. Do not call for a load
that was not shown, and do not call before the OTP is verified.

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |
| `load_id` | Agent decides | string | `LD00859` | yes |

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/negotiations`
- Body:
```json
{
  "run_id": "{{$var:<tool>.run_id}}",
  "load_id": "{{$var:<tool>.load_id}}"
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `load_id` | Agent | echo |
| `offer` | Agent | integer, dollars |
| `say` | Agent | speak this |

**Retries / blocking / end call.** Retries: 1 (idempotent — reopening returns the
same offer). Blocking: yes. End call: no.

**Additional node.** None.

---

## 5. `counter_offer` — POST `/negotiations/counter`

**Description (agent).** Send the carrier's counter-offer (a dollar amount) and
get `accept` / `counter` / `close`. Call once per carrier counter, after
`open_negotiation`. On `close`, end the call — do not hand off. Do not call to
open a negotiation, and do not send a number the carrier did not say.

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |
| `carrier_ask` | Agent decides | integer | `1750` | yes |

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/negotiations/counter`
- Body:
```json
{
  "run_id": "{{$var:<tool>.run_id}}",
  "carrier_ask": {{$var:<tool>.carrier_ask}}
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `action` | Agent | `accept` \| `counter` \| `close` |
| `amount` | Agent | integer, dollars |
| `result_code` | Agent (routing) | `negotiation_failed` on `close`, else `null` — hidden from the agent, counted by the platform |
| `say` | Agent | speak this |

Nothing else is returned — no ceiling, no gap, no step. The server owns all of
that.

**Retries / blocking / end call.** Retries: 1 — safe now: the server replays the
same decision when the same `carrier_ask` arrives again within 15 s, without
consuming a round. Blocking: yes. End call: no (on `close`, the agent ends the
call directly — no handoff).

**Additional node.** None.

---

## 6. `book_load` — POST `/bookings`

**Description (agent).** Book the load at the rate the carrier accepted. Call
only after `counter_offer` returned `accept`. Do not call before an accepted
rate, and do not pass a rate — the server uses the rate it stored, never one the
agent supplies.

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/bookings`
- Body:
```json
{
  "run_id": "{{$var:<tool>.run_id}}"
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `status` | Agent | `booked` \| `booking_pending` |
| `booking_ref` | Agent (rep-facing) | confirmation ref, or `null` when pending |
| `result_code` | Agent (routing) | `booked` \| `booking_pending` |
| `say` | Agent | speak this |

**Retries / blocking / end call.** Retries: 1 — safe now: the server stores the
booking per `run_id`, so a retry returns the same answer instead of a second
`LOAD_BOOK`. Blocking: yes. End call: no — `handoff` still follows; on
`booking_pending` the agent says a rep confirms, then hands off.

**Additional node.** None.

---

## 7. `handoff` — POST `/handoff`

**Description (agent).** Hand the call to a senior rep. Call only to close out a
booked or pending booking, to route a carrier with no contact on file, or when
the caller asks for a human. Do not call it for a closed negotiation or an empty
search — those end the call on their own.

**Message (said before the tool runs).** `Connecting you with a rep now, hang
tight.`

**Parameters.**

| name | source | type | example | required |
|---|---|---|---|---|
| `run_id` | Fixed value (Run ID chip) | string | — | yes |
| `reason` | Agent decides | string | `booking_pending` | no |

`reason` is optional. Valid values: `booked`, `booking_pending`,
`negotiation_failed`, `onboarding`, `fmcsa_rejected`, `requested`. An
unrecognised one is recorded as `requested`.

**Child node (Webhook).**

- Method: `POST`
- URL: `{{SERVICE_BASE_URL}}/handoff`
- Body:
```json
{
  "run_id": "{{$var:<tool>.run_id}}",
  "reason": "{{$var:<tool>.reason}}"
}
```

**Response fields.**

| field | audience | note |
|---|---|---|
| `ticket_id` | Agent (rep-facing) | ticket reference |
| `say` | Agent | speak this |

**Retries / blocking / end call.** Retries: 1 (idempotent — a repeat opens a
second ticket, harmless). Blocking: yes. End call: **yes** — the call is cut as
soon as the tool returns; the caller only hears the `Message` above.

**Additional node.** None.

---

## Call order

`verify_carrier` (already built) → `send_otp` → `verify_otp` → `search_loads` →
`open_negotiation` → `counter_offer` → `book_load` → `handoff`.

Gating recap: `verify_carrier`, `send_otp`, `verify_otp` and `handoff` run
without a verified OTP (handoff is the escape hatch). `search_loads`,
`open_negotiation`, `counter_offer` and `book_load` all require the OTP to be
verified for the same `run_id`.

## Schema capture (`run_id = "schema-sample"`)

The HappyRobot **Generate** button fires a real request to capture a tool's
response shape, and there is no session for it. When `run_id` is exactly
`schema-sample`, every tool returns a 200 with a full sample response (dummy
values, e.g. `LD-SAMPLE`, code `000000`, `sample@example.com`) instead of
touching state. `X-API-Key` is still required.

## End call — only `handoff`

`handoff` is the only tool with **end call = yes**. "End call after this tool"
cuts the call the moment the tool returns, with no agent turn — the caller hears
only the tool's `Message`, which is said *before* the call runs. That is why
`handoff` carries a fixed `Message` ("Connecting you with a rep now, hang
tight.") and does not depend on the result.

Every other "this call is over" moment is *conditional*, so it stays with the
prompt rather than a blanket end call:

- `verify_carrier` (`eligible: false`) — the agent reads the polite `say` and
  ends. Conditional, so no end call.
- `search_loads` (`no_match`) — the agent takes details and ends; the demand is
  recorded server-side. No handoff, no end call.
- `counter_offer` (`action: close`) — the agent ends the call directly; the gap
  is recorded server-side (`negotiation_failed`). No handoff, no end call.
- `book_load` — no end call: the agent must say `booked` vs `booking_pending`
  from the result, and `handoff` follows either way.
