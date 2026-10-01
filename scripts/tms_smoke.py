#!/usr/bin/env python3
"""Read-only probe of the live legacy TMS.

This script exists to answer the questions docs/tms-protocol.md leaves open:
what the real result ceiling is, whether ``PICKUP_DT`` is the right filter
name, whether ``FLATBED`` is a real equipment value, and how the server pads
values today. It runs the encoder and the parser against the real server so
that the fixture-driven tests are anchored to something observed.

**It is read-only by construction.** ``LOAD_BOOK`` is refused by a guard,
because every successful booking permanently consumes a load for our token.

Usage:
    .venv/bin/python scripts/tms_smoke.py
    .venv/bin/python scripts/tms_smoke.py --out docs/smoke-2026-10-01.txt
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow `python scripts/tms_smoke.py`
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.tms.client import TmsClient  # noqa: E402
from app.tms.protocol import (  # noqa: E402
    TmsError,
    TmsFault,
    encode_request,
    redact,
)

#: Commands this script is allowed to send. Anything else is a bug.
READ_ONLY_COMMANDS = frozenset({"DEBUG_ECHO", "LOAD_QUERY", "LOAD_GET"})

#: A date no sensible dataset would contain, used to detect a filter name the
#: server silently discards (an unknown field looks exactly like "no filter").
IMPOSSIBLE_DATE = "19990101000000"

#: Stands in for the token when echoing a request frame.
TOKEN_PLACEHOLDER = "<redacted>"


def guard(command: str) -> None:
    """Refuse anything that could change state on the server."""
    if command not in READ_ONLY_COMMANDS:
        raise SystemExit(
            f"refusing to run {command}: scripts/tms_smoke.py is read-only"
        )


@dataclass
class Result:
    """What one probe saw."""

    label: str
    command: str
    fields: dict[str, object]
    records: list[dict[str, str]] | None = None
    fault: TmsFault | None = None
    milliseconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.fault is None


class Reporter:
    """Collects output so it can go to stdout and to a file at the same time."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def probe(
    client: TmsClient,
    reporter: Reporter,
    index: int,
    label: str,
    command: str,
    fields: dict[str, object],
) -> Result:
    """Run one read-only command and print what came back."""
    guard(command)

    # Build the frame with a placeholder token: this is the real encoder, but
    # the real token never reaches the terminal or the report file.
    frame = encode_request(command, TOKEN_PLACEHOLDER, fields)
    reporter.emit(f"── {index}. {label}")
    reporter.emit(f"   request : {redact(frame.decode('ascii')).strip()}")

    started = time.monotonic()
    try:
        records = client.execute(command, fields)
        result = Result(label, command, fields, records=records)
    except TmsFault as fault:
        result = Result(label, command, fields, fault=fault)
    result.milliseconds = (time.monotonic() - started) * 1000

    if result.ok:
        reporter.emit(f"   result  : {len(records)} record(s) in {result.milliseconds:.0f} ms")
    else:
        reporter.emit(f"   result  : {type(result.fault).__name__} in {result.milliseconds:.0f} ms")
        reporter.emit(f"   detail  : {result.fault}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="also write the report to this file")
    parser.add_argument("--verbose", action="store_true", help="log every attempt")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Connection warnings are expected while probing; they are not news.
    logging.getLogger("app.tms.client").setLevel(logging.INFO if args.verbose else logging.ERROR)

    settings = get_settings()
    if not (settings.tms_host and settings.tms_port and settings.tms_token_value):
        print(
            "No TMS credentials found.\n"
            "Copy .env.example to .env and fill in TMS_HOST, TMS_PORT and TMS_TOKEN.",
            file=sys.stderr,
        )
        return 2

    client = TmsClient.from_settings(settings)
    reporter = Reporter()

    reporter.emit("=" * 72)
    reporter.emit("TMS smoke test (read-only)")
    reporter.emit(f"host     : {settings.tms_host}:{settings.tms_port}")
    reporter.emit(f"token    : <redacted, {len(settings.tms_token_value)} chars>")
    reporter.emit(f"deadline : {settings.tms_read_deadline_seconds}s, {settings.tms_max_attempts} attempts")
    reporter.emit(f"started  : {datetime.now(timezone.utc).isoformat()}")
    reporter.emit(f"fixtures : {REPO_ROOT / 'tests' / 'fixtures' / 'tms_samples.txt'}")
    reporter.emit("=" * 72)
    reporter.emit()

    # 1. Encoder sanity. DEBUG_ECHO bypasses fault injection, so it says
    #    nothing about the server's health -- only that our frame is well formed.
    echo = probe(
        client, reporter, 1, "DEBUG_ECHO (encoder sanity only)",
        "DEBUG_ECHO", {"MSG": "smoke"},
    )
    reporter.emit()

    # 2. Baseline: what the server offers at all.
    baseline = probe(
        client, reporter, 2, "LOAD_QUERY baseline",
        "LOAD_QUERY", {"EQTYPE": "DRY_VAN", "MAX_RESULTS": 5},
    )
    reporter.emit()

    baseline_ids = [record.get("LOAD_ID") for record in (baseline.records or [])]

    # 3. LOAD_GET on a load that actually exists right now.
    if baseline_ids and baseline_ids[0]:
        detail = probe(
            client, reporter, 3, f"LOAD_GET {baseline_ids[0]}",
            "LOAD_GET", {"LOAD_ID": baseline_ids[0]},
        )
    else:
        detail = None
        reporter.emit("── 3. LOAD_GET — skipped, the baseline returned no loads")
    reporter.emit()

    # 4. Is FLATBED a real equipment value? An unknown one is not an error.
    flatbed = probe(
        client, reporter, 4, "LOAD_QUERY EQTYPE:FLATBED",
        "LOAD_QUERY", {"EQTYPE": "FLATBED", "MAX_RESULTS": 5},
    )
    reporter.emit()

    # 5. Is PICKUP_DT the right filter name? An unknown field is silently
    #    discarded, so an impossible date must return *fewer* records than the
    #    baseline. If it returns the same, the field is being ignored.
    date_filter = probe(
        client, reporter, 5, f"LOAD_QUERY PICKUP_DT:{IMPOSSIBLE_DATE} (must match nothing)",
        "LOAD_QUERY", {"PICKUP_DT": IMPOSSIBLE_DATE, "MAX_RESULTS": 5},
    )
    reporter.emit()

    # 5b. And with a date we know is in the data, to see if it selects.
    date_match = None
    if baseline.records:
        sample = next(
            (record for record in baseline.records if record.get("PICKUP_DT")),
            None,
        )
        if sample:
            day = sample["PICKUP_DT"][:8]
            date_match = probe(
                client, reporter, 6, f"LOAD_QUERY PICKUP_DT:{day}000000 (a day we know exists)",
                "LOAD_QUERY", {"PICKUP_DT": f"{day}000000", "MAX_RESULTS": 5},
            )
            reporter.emit()

    # 6. The real ceiling. MAX_RESULTS:50 either returns fewer than 50 (that is
    #    the ceiling) or exactly 50 (the server capped us -- probe higher).
    ceiling = probe(
        client, reporter, 7, "LOAD_QUERY MAX_RESULTS:50 (ceiling probe)",
        "LOAD_QUERY", {"EQTYPE": "DRY_VAN", "MAX_RESULTS": 50},
    )
    higher = None
    if ceiling.records is not None and len(ceiling.records) >= 50:
        reporter.emit()
        higher = probe(
            client, reporter, 8, "LOAD_QUERY MAX_RESULTS:200 (bracketing the ceiling)",
            "LOAD_QUERY", {"EQTYPE": "DRY_VAN", "MAX_RESULTS": 200},
        )
    reporter.emit()

    # --- Findings -------------------------------------------------------
    reporter.emit("=" * 72)
    reporter.emit("FINDINGS")
    reporter.emit("=" * 72)

    if echo.ok and echo.records:
        reporter.emit(f"encoder   : accepted, server echoed {echo.records[0]}")
    else:
        reporter.emit(f"encoder   : FAILED against the live server -> {echo.fault}")

    if baseline.records:
        first = baseline.records[0]
        reporter.emit("padding   : first record exactly as received")
        reporter.emit(f"            LOAD_ID={first.get('LOAD_ID')!r}")
        reporter.emit(f"            RATE={first.get('RATE')!r} MILES={first.get('MILES')!r}")
    else:
        reporter.emit("padding   : no baseline record to inspect")

    if detail is not None and detail.ok and detail.records:
        record = detail.records[0]
        present = "present" if "MAX_BUY" in record else "ABSENT"
        reporter.emit(f"detail    : LOAD_GET ok; MAX_BUY {present} (value {record.get('MAX_BUY')!r})")
    else:
        reason = detail.fault if detail is not None else "skipped, no baseline load"
        reporter.emit(f"detail    : LOAD_GET returned no record ({reason})")

    if flatbed.ok:
        verdict = "a real equipment value" if flatbed.records else "no matches (unknown value or no stock)"
        reporter.emit(f"FLATBED   : {len(flatbed.records or [])} record(s) -> {verdict}")
    else:
        reporter.emit(f"FLATBED   : {type(flatbed.fault).__name__} -> {flatbed.fault}")

    if date_filter.ok and date_match is not None and date_match.ok:
        impossible = len(date_filter.records or [])
        known = len(date_match.records or [])
        ignored = impossible == len(baseline.records or []) and impossible > 0
        reporter.emit(f"PICKUP_DT : impossible date -> {impossible} record(s), known date -> {known}")
        reporter.emit(
            f"            filter is {'IGNORED, so the name is wrong' if ignored else 'honoured'}"
        )
    elif date_filter.fault is not None:
        reporter.emit(f"PICKUP_DT : {type(date_filter.fault).__name__} -> {date_filter.fault}")

    if ceiling.ok:
        note = f"{len(ceiling.records or [])} record(s) for MAX_RESULTS:50"
        if higher is not None and higher.ok:
            note += f", {len(higher.records or [])} for MAX_RESULTS:200"
        reporter.emit(f"ceiling   : {note}")

    faults = [
        f"{r.label}: {type(r.fault).__name__}"
        for r in [echo, baseline, flatbed, date_filter, ceiling, higher]
        if r is not None and not r.ok
    ]
    reporter.emit(f"faults    : {', '.join(faults) if faults else 'none observed'}")
    reporter.emit()
    reporter.emit("No LOAD_BOOK was sent. This script is read-only by construction.")

    if args.out:
        reporter.save(args.out)
        print(f"\nreport written to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
