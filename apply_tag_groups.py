#!/usr/bin/env python3
"""Apply classified tag-to-group mappings to Eagle's metadata.json.

Reads .tag_group_classification.json (built by classify_tags_to_groups.py)
and writes each tag into the corresponding group's "tags" list inside the
library metadata.json file.

Safety:
  - Eagle.app must NOT be running (changes get overwritten otherwise)
  - metadata.json is backed up to .tag_groups_backup_<timestamp>.json
    BEFORE any edit; rollback by copying the backup back.
  - --dry-run shows what will change without writing.
  - Validates JSON parses after write and that all groups still exist.

Usage:
  python3 apply_tag_groups.py --dry-run
  python3 apply_tag_groups.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LIBRARY = Path(
    os.path.expanduser("~/Eagle/MyLibrary.library")  # override via EAGLE_LIBRARY
)
CLASSIFICATION_PATH = SCRIPT_DIR / ".tag_group_classification.json"


def eagle_running() -> bool:
    """Return True if Eagle.app is currently running."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "Eagle.app/Contents/MacOS/Eagle"],
            stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="Apply tag-group classification to metadata.json")
    p.add_argument("--library", default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)))
    p.add_argument("--classification", default=str(CLASSIFICATION_PATH))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-ungrouped", action="store_true",
                   help="Also collect 'ungrouped' tags (not assigned anywhere)")
    p.add_argument("--force", action="store_true",
                   help="Proceed even if Eagle.app is running (DANGEROUS)")
    args = p.parse_args()

    library = Path(args.library)
    meta_path = library / "metadata.json"
    if not meta_path.exists():
        print(f"❌ metadata.json not found: {meta_path}", file=sys.stderr)
        return 1
    classification_path = Path(args.classification)
    if not classification_path.exists():
        print(f"❌ classification not found: {classification_path}", file=sys.stderr)
        print("   Run classify_tags_to_groups.py first.", file=sys.stderr)
        return 1

    if eagle_running() and not args.force and not args.dry_run:
        print("❌ Eagle.app is running. Quit Eagle first (Cmd+Q) or use --force.", file=sys.stderr)
        return 1

    with open(classification_path, encoding="utf-8") as f:
        cls = json.load(f)
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    groups = meta.get("tagsGroups", []) or []
    if not groups:
        print("❌ no tagsGroups in metadata.json — create groups in Eagle first.", file=sys.stderr)
        return 1
    name_to_group = {g["name"]: g for g in groups}

    # Aggregate tag → group
    assignments: dict[str, list[str]] = {g["name"]: [] for g in groups}
    skipped_unknown_group: list[tuple[str, str]] = []
    skipped_ungrouped = 0

    for lang in ("en", "ko"):
        for tag, group_name in (cls.get("classification", {}).get(lang, {}) or {}).items():
            if group_name == "ungrouped":
                skipped_ungrouped += 1
                continue
            if group_name not in name_to_group:
                skipped_unknown_group.append((tag, group_name))
                continue
            assignments[group_name].append(tag)

    # Preserve any tags already in the group (don't overwrite manual additions)
    final_assignments: dict[str, list[str]] = {}
    for g in groups:
        existing = list(g.get("tags") or [])
        new = assignments[g["name"]]
        # Union, preserving order: existing first, then new not in existing
        existing_set = set(existing)
        combined = existing + [t for t in new if t not in existing_set]
        final_assignments[g["name"]] = combined

    print(f"📚 Library: {library.name}")
    print(f"📑 Groups: {len(groups)}")
    print(f"📋 Classification: {classification_path.name}\n")

    print("=== Assignment plan ===")
    for g in groups:
        existing = list(g.get("tags") or [])
        new = assignments[g["name"]]
        final = final_assignments[g["name"]]
        added = len(final) - len(existing)
        print(f"  {g['name']:<22} existing={len(existing)}  +new={added}  →total={len(final)}")

    if skipped_unknown_group:
        print(f"\n⚠️  unknown group names ({len(skipped_unknown_group)}, skipped):")
        for t, g in skipped_unknown_group[:5]:
            print(f"    {t!r} → {g!r}")
    if skipped_ungrouped:
        print(f"\n⏩ ungrouped (left unassigned): {skipped_ungrouped}")

    if args.dry_run:
        print("\n(dry-run; no changes written.)")
        return 0

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SCRIPT_DIR / f".tag_groups_backup_{ts}.json"
    shutil.copy2(meta_path, backup_path)
    print(f"\n💾 backup: {backup_path.name}")

    # Apply
    for g in meta.get("tagsGroups", []):
        g["tags"] = final_assignments.get(g["name"], list(g.get("tags") or []))
    meta["modificationTime"] = int(datetime.now().timestamp() * 1000)

    # Atomic write: write to temp then rename
    tmp_path = meta_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    # Verify JSON parses
    with open(tmp_path, encoding="utf-8") as f:
        verified = json.load(f)
    assert len(verified.get("tagsGroups", [])) == len(groups), "group count mismatch"
    os.replace(tmp_path, meta_path)
    print(f"✅ wrote: {meta_path.name}")

    print(f"\n=== next steps ===")
    print(f"  1. Eagle.app 실행 (보통은 자동으로 새 그룹 반영)")
    print(f"  2. 좌측 사이드바에서 그룹 펼쳐서 태그 들어갔는지 확인")
    print(f"  3. 잘못된 경우: cp {backup_path.name} {meta_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
