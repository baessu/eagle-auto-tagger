#!/usr/bin/env python3
"""Migrate Eagle library tags using a canonical map.

Steps:
  1. Load .tag_canonical_map.json (built by build_canonical_map.py)
  2. Compute transitive closure (a→b→c collapses to a→c)
  3. Snapshot all current item tags to .tag_backup_<timestamp>.jsonl
  4. For each item, apply mapping (dedup, preserve order)
  5. Write back via Eagle API /api/item/update with round-trip verification

Safety:
  - Backup is mandatory and written BEFORE any update
  - --dry-run prints planned changes without touching Eagle
  - --limit N processes only first N items (sample run)
  - Failures are logged; the rest of the run continues
  - All mappings are language-agnostic (en+ko collapsed into one dict)

Usage:
  python3 migrate_tags.py --dry-run            # preview top 20 changes
  python3 migrate_tags.py --dry-run --limit 50 # preview with count
  python3 migrate_tags.py --limit 10           # apply to first 10 items
  python3 migrate_tags.py                      # full migration
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LIBRARY = Path(
    os.path.expanduser("~/Eagle/MyLibrary.library")  # override via EAGLE_LIBRARY
)
CANONICAL_MAP_PATH = SCRIPT_DIR / ".tag_canonical_map.json"
EAGLE_BASE = "http://localhost:41595"


def eagle_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{EAGLE_BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def eagle_get(path: str) -> dict:
    with urllib.request.urlopen(f"{EAGLE_BASE}{path}", timeout=30) as r:
        return json.load(r)


def load_map() -> dict[str, str]:
    """Load canonical map and merge en+ko into a single language-agnostic dict."""
    with open(CANONICAL_MAP_PATH, encoding="utf-8") as f:
        d = json.load(f)
    en = d.get("map", {}).get("en", {}) or {}
    ko = d.get("map", {}).get("ko", {}) or {}
    merged = dict(en)
    for k, v in ko.items():
        merged[k] = v  # ko entries override en if any collision
    return merged


def close_map(m: dict[str, str]) -> dict[str, str]:
    """Compute transitive closure: a→b→c becomes a→c. Cycles end at the
    first repeated node."""
    closed: dict[str, str] = {}
    for k in m:
        v = k
        seen: set[str] = set()
        while True:
            nxt = m.get(v, v)
            if nxt == v or v in seen:
                break
            seen.add(v)
            v = nxt
        closed[k] = v
    return closed


def transform_tags(tags: list[str], closed: dict[str, str]) -> list[str]:
    """Apply mapping and dedup, preserving first-occurrence order."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        new = closed.get(t, t)
        if new not in seen:
            out.append(new)
            seen.add(new)
    return out


def snapshot_items(library: Path) -> list[dict]:
    """Build [{id, name, tags}] for every non-deleted tagged item."""
    out: list[dict] = []
    for meta in sorted(library.glob("images/*/metadata.json")):
        try:
            with open(meta, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("isDeleted"):
            continue
        tags = d.get("tags") or []
        if not tags:
            continue
        out.append({
            "id": d["id"],
            "name": d.get("name", ""),
            "tags": list(tags),
        })
    return out


def write_backup(items: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def update_item_tags(item_id: str, tags: list[str]) -> tuple[bool, str]:
    """Return (ok, message). On success, verifies round-trip."""
    try:
        result = eagle_post("/api/item/update", {"id": item_id, "tags": tags})
    except (urllib.error.URLError, OSError) as e:
        return False, f"network: {e}"
    if result.get("status") != "success":
        return False, f"eagle: {result.get('status')}"
    got = set(result.get("data", {}).get("tags", []))
    want = set(tags)
    if got != want:
        missing = want - got
        extra = got - want
        return False, f"mismatch: missing={list(missing)[:3]}, extra={list(extra)[:3]}"
    return True, "ok"


def main() -> int:
    p = argparse.ArgumentParser(description="Migrate Eagle library tags with canonical map")
    p.add_argument("--library", default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)))
    p.add_argument("--map", default=str(CANONICAL_MAP_PATH), help="Canonical map JSON path")
    p.add_argument("--dry-run", action="store_true", help="Preview changes, do not write")
    p.add_argument("--limit", type=int, default=0, help="Process first N items (0=all)")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel writers (default 4)")
    p.add_argument("--backup-dir", default=str(SCRIPT_DIR), help="Backup file directory")
    args = p.parse_args()

    library = Path(args.library)
    if not library.exists():
        print(f"❌ library not found: {library}", file=sys.stderr)
        return 1
    if not Path(args.map).exists():
        print(f"❌ canonical map not found: {args.map}", file=sys.stderr)
        return 1

    print(f"📚 Library: {library.name}")
    print(f"🗺  Map: {args.map}")

    # Eagle reachability check (only when actually writing)
    if not args.dry_run:
        try:
            info = eagle_get("/api/application/info")
            print(f"✅ Eagle: v{info['data']['version']}")
        except (urllib.error.URLError, KeyError) as e:
            print(f"❌ Eagle API unreachable: {e}", file=sys.stderr)
            return 1

    raw_map = load_map()
    closed = close_map(raw_map)
    n_changes_in_map = sum(1 for k, v in closed.items() if k != v)
    print(f"📋 Map: {len(closed)} entries, {n_changes_in_map} non-self mappings (post-closure)")

    items = snapshot_items(library)
    print(f"📦 Tagged items: {len(items)}")

    # Plan diffs
    plans: list[tuple[dict, list[str], list[str]]] = []  # (item, old, new)
    for it in items:
        new_tags = transform_tags(it["tags"], closed)
        if new_tags != it["tags"]:
            plans.append((it, it["tags"], new_tags))

    print(f"🔀 Items needing update: {len(plans)} of {len(items)}")
    if not plans:
        print("nothing to do")
        return 0

    # Stats: how many tags collapsed, average reduction
    total_before = sum(len(b) for _, b, _ in plans)
    total_after = sum(len(a) for _, _, a in plans)
    print(f"   total tag instances: {total_before:,} → {total_after:,} "
          f"(−{total_before - total_after:,}, {(total_before - total_after) / max(1, total_before) * 100:.1f}%)")

    if args.dry_run:
        sample = plans[:min(args.limit or 20, len(plans))]
        print(f"\n--- preview (first {len(sample)} changes) ---")
        for it, old, new in sample:
            removed = [t for t in old if t not in set(new)]
            added = [t for t in new if t not in set(old)]
            print(f"  {it['id']}  {it['name'][:40]!r}")
            print(f"    −{len(old):>2}  removed: {removed[:6]}{'…' if len(removed) > 6 else ''}")
            print(f"    +{len(new):>2}  added:   {added[:6]}{'…' if len(added) > 6 else ''}")
        print(f"\n(dry-run; no changes written. Run without --dry-run to apply.)")
        return 0

    # Apply
    if args.limit:
        plans = plans[: args.limit]
        print(f"\n🔢 Limited to first {len(plans)} items")

    # Backup BEFORE any writes
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = Path(args.backup_dir) / f".tag_backup_{ts}.jsonl"
    backup_items = [{"id": it["id"], "name": it["name"], "tags": it["tags"]}
                    for it, _, _ in plans]
    write_backup(backup_items, backup_path)
    print(f"💾 Backup: {backup_path} ({backup_path.stat().st_size:,} bytes, {len(backup_items)} items)")

    progress_path = SCRIPT_DIR / ".tag_migration_progress.jsonl"
    failed_path = SCRIPT_DIR / ".tag_migration_failed.jsonl"
    ok = 0
    err = 0
    t0 = time.time()

    def worker(payload: tuple[dict, list[str], list[str]]) -> tuple[dict, list[str], bool, str]:
        it, _, new_tags = payload
        success, msg = update_item_tags(it["id"], new_tags)
        return it, new_tags, success, msg

    with cf.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futures = [ex.submit(worker, plan) for plan in plans]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            it, new_tags, success, msg = fut.result()
            if success:
                ok += 1
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "id": it["id"], "name": it["name"],
                        "n_tags": len(new_tags), "ts": time.time(),
                    }, ensure_ascii=False) + "\n")
                if i % 50 == 0 or i == len(plans):
                    print(f"  [{i}/{len(plans)}] ok={ok} err={err} ({time.time() - t0:.0f}s elapsed)")
            else:
                err += 1
                print(f"  [{i}/{len(plans)}] ❌ {it['id']} — {msg}")
                with open(failed_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "id": it["id"], "name": it["name"],
                        "error": msg, "ts": time.time(),
                    }, ensure_ascii=False) + "\n")

    dt = time.time() - t0
    print(f"\n━━━ migration done in {dt:.1f}s ━━━")
    print(f"  success: {ok}")
    print(f"  failed:  {err}")
    print(f"  backup:  {backup_path.name}")
    if err:
        print(f"  → failures logged to {failed_path.name}")
        print(f"  → rollback: see backup file for original tags")
    return 0 if err == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
