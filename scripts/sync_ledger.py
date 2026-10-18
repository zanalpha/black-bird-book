#!/usr/bin/env python3
"""Copy the live Black Bird book into ledger.json and refresh the live copy.

The topic must match LEDGER_TOPIC in src/lib/remote-ledger.ts.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TOPIC = "bbird-bcf9e2ba1a87435005e38f710acbb251"
NTFY_URL = f"https://ntfy.sh/{TOPIC}"
ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "ledger.json"
MAX_TOMBSTONES = 2000
REFRESH_REMAINING_SEC = 90 * 60


def time_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def empty_book() -> dict:
    return {"transactions": [], "deleted": []}


def load_book(path: Path) -> dict:
    if not path.exists():
        return empty_book()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_book()
    if not isinstance(raw, dict):
        return empty_book()
    transactions = raw.get("transactions")
    deleted = raw.get("deleted")
    return {
        "transactions": transactions if isinstance(transactions, list) else [],
        "deleted": deleted if isinstance(deleted, list) else [],
    }


def canonical(book: dict) -> str:
    return json.dumps(
        {"transactions": book["transactions"], "deleted": book["deleted"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def consider_tx(items: dict[str, dict], item: object, prefer_on_tie: bool) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return
    updated = time_ms(item.get("updatedAt"))
    if updated is None:
        return
    previous = items.get(item["id"])
    if previous is None:
        items[item["id"]] = item
        return
    previous_ms = time_ms(previous.get("updatedAt")) or 0
    if updated > previous_ms or (updated == previous_ms and prefer_on_tie):
        items[item["id"]] = item


def consider_tomb(items: dict[str, str], item: object, prefer_on_tie: bool) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return
    at = item.get("at")
    updated = time_ms(at)
    if updated is None or not isinstance(at, str):
        return
    previous = items.get(item["id"])
    if previous is None:
        items[item["id"]] = at
        return
    previous_ms = time_ms(previous) or 0
    if updated > previous_ms or (updated == previous_ms and prefer_on_tie):
        items[item["id"]] = at


def merge(base: dict, incoming: dict) -> dict:
    transactions: dict[str, dict] = {}
    for item in base["transactions"]:
        consider_tx(transactions, item, True)
    for item in incoming["transactions"]:
        consider_tx(transactions, item, False)

    deleted: dict[str, str] = {}
    for item in base["deleted"]:
        consider_tomb(deleted, item, True)
    for item in incoming["deleted"]:
        consider_tomb(deleted, item, False)

    for item_id, at in list(deleted.items()):
        current = transactions.get(item_id)
        if current is None or (time_ms(at) or 0) >= (time_ms(current.get("updatedAt")) or 0):
            transactions.pop(item_id, None)
        else:
            deleted.pop(item_id, None)

    def tx_key(item: dict) -> tuple:
        return (str(item.get("date", "")), str(item.get("createdAt", "")), str(item.get("id", "")))

    tombs = [{"id": item_id, "at": at} for item_id, at in deleted.items()]
    tombs.sort(key=lambda item: (-(time_ms(item["at"]) or 0), item["id"]))
    return {
        "transactions": sorted(transactions.values(), key=tx_key, reverse=True),
        "deleted": tombs[:MAX_TOMBSTONES],
    }


def fetch_json(url: str, timeout: int = 20):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def live_snapshots() -> tuple[list[dict], dict | None]:
    body = fetch_json(f"{NTFY_URL}/json?poll=1&since=12h")
    snapshots: list[dict] = []
    latest: dict | None = None
    now = time.time()
    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("event") != "message":
            continue
        attachment = message.get("attachment") or {}
        expires = attachment.get("expires") or 0
        url = attachment.get("url")
        if not isinstance(url, str) or not isinstance(expires, (int, float)) or expires <= now:
            continue
        try:
            payload = json.loads(fetch_json(url))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        transactions = payload.get("transactions")
        deleted = payload.get("deleted")
        if not isinstance(transactions, list) or not isinstance(deleted, list):
            continue
        book = {"transactions": transactions, "deleted": deleted}
        snapshots.append(book)
        latest = {"expires": expires, "book": book}
    return snapshots, latest


def publish(book: dict) -> None:
    data = canonical(book).encode("utf-8")
    request = urllib.request.Request(
        NTFY_URL,
        data=data,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Filename": "ledger.json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status >= 300:
            raise RuntimeError(f"publish failed: {response.status}")


def main() -> int:
    base = load_book(LEDGER_PATH)
    try:
        snapshots, latest = live_snapshots()
    except OSError as error:
        print(f"live book unavailable: {error}", file=sys.stderr)
        return 0

    merged = base
    for snapshot in snapshots:
        merged = merge(merged, snapshot)

    if canonical(merged) != canonical(base):
        LEDGER_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("ledger.json updated")
    else:
        print("ledger.json unchanged")

    remaining = 0 if latest is None else latest["expires"] - time.time()
    stale = latest is None or canonical(latest["book"]) != canonical(merged)
    if stale or remaining < REFRESH_REMAINING_SEC:
        publish(merged)
        print("live book refreshed")
    else:
        print("live book still fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
