# Transcription context — agent voice configuration

Paste the block below into the agent's **transcription context** field in the voice
configuration. It is model-visible text, and it is the one place in the workflow that
asserted a fixed length for MC numbers.

## The problem this fixes

The field read:

> Callers are US truck drivers and carrier dispatchers looking for freight loads. They will
> say **MC numbers (6 to 7 digits)**, US city and state names, equipment types (dry van,
> reefer, flatbed) and dollar rates.

That is wrong, and it is a claim rather than an example: MC numbers have no fixed length.
The reviewer carrier's MC is `15735` — five digits — and a carrier on a real call heard
*"Please provide your six-digit MC number"*, which is a number the caller cannot give.
The service accepts 1–8 digits (`^\d{1,8}$`), so nothing downstream was broken; only the
agent was.

## The replacement

```
Callers are US truck drivers and carrier dispatchers looking for freight loads. They will say MC numbers (digits only, no fixed length), US city and state names, equipment types (dry van, reefer, flatbed) and dollar rates.
```

## Where else this text lives

The same paragraph appears in this repository in `docs/workflow-sample.json`
(`nodes[2].configuration.transcription_context`), where it has already been corrected. That
copy is a captured sample and changing it does not change the platform — **this file is the
one to paste from.**

## The other three places an MC length could hide

Checked, so the next reader does not have to:

| Place | Finding |
|---|---|
| `docs/prompt-live.md` | No length claim. Two six-digit *examples* (`107012`) were replaced with a neutral five-digit one, and the rule is now stated outright: *"MC numbers are not a fixed length — never say how many digits one has."* |
| Server `say` strings (`app/api/phrasing.py`) | No mention of an MC length. `MC_INVALID` and `CARRIER_NOT_FOUND` both say "MC number" and nothing about digits. |
| Server validation (`app/fmcsa/client.py`) | `^\d{1,8}$` — **1 to 8 digits**. It does not require six, and `15735` passes. No change needed. |

The `six digit` strings in `app/api/phrasing.py` are all about the **OTP code**, which really
is six digits. Those are correct and stay.
