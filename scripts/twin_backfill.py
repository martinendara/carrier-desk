"""Backfill HappyRobot Twin from the JSONL event log.

Reads `events.jsonl` and uploads the rows Twin does not have yet. Idempotent:
each JSONL line maps to a deterministic Twin row id (see `app.twin.event_id`),
and a local checkpoint records how many lines have already been uploaded, so a
re-run only sends the tail.

Usage:
    .venv/bin/python scripts/twin_backfill.py [--events var/events.jsonl]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from app.config import get_settings
from app.events.models import Event
from app.twin import _clean_key, _is_duplicate, build_row

CHECKPOINT = "var/twin_backfill_checkpoint"


def _load_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(Event(**json.loads(line)))
    return events


def _checkpoint() -> int:
    try:
        return int(Path(CHECKPOINT).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_checkpoint(n: int) -> None:
    Path(CHECKPOINT).write_text(str(n), encoding="utf-8")


def _upload_one(settings, client, row: dict) -> bool:
    """Upload one row. False when it is already present (idempotent skip)."""
    url = f"{settings.twin_base_url.rstrip('/')}/twin/tables/{settings.twin_table}/rows"
    headers = {
        "Authorization": f"Bearer {_clean_key(settings.hr_platform_api_key.get_secret_value())}",
        "Content-Type": "application/json",
    }
    response = client.post(url, json={"values": row}, headers=headers)
    if response.status_code == 409 or _is_duplicate(response):
        return False
    response.raise_for_status()
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", default="var/events.jsonl")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.twin_enabled:
        print("TWIN_ENABLED is false; nothing to backfill.")
        return 0

    events = _load_events(Path(args.events))
    already = min(_checkpoint(), len(events))
    pending = events[already:]
    if not pending:
        print(f"nothing new: {already} rows already uploaded")
        return 0

    rows = [build_row(event) for event in pending]
    sent = 0
    with httpx.Client(timeout=settings.twin_timeout_seconds) as client:
        for row in rows:
            try:
                sent += 1 if _upload_one(settings, client, row) else 1
            except httpx.HTTPError as exc:
                print(f"stopping at {row['id']}: {type(exc).__name__}: {exc}")
                break

    _save_checkpoint(already + sent)
    print(f"uploaded {sent} row(s); total {already + sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
