#!/usr/bin/env python3
"""Build a canonical tag map for the Eagle library.

Strategy:
  1. Scan all metadata.json on disk → frequency-rank all unique tags
  2. Pick the top N tags per language (default 800) where freq >= 2
  3. Send each language's list to Claude (vision-less call via `claude -p`)
     and ask for a {alias: canonical} mapping
  4. Save to .tag_canonical_map.json

Output structure:
  {
    "generated_at": "2026-05-12T12:34:56",
    "library": "MyLibrary.library",
    "stats": {"en_input": 800, "ko_input": 800, "en_changes": 312, "ko_changes": 287},
    "map": {
        "en": {"alias": "canonical", ...},
        "ko": {"alias": "canonical", ...}
    }
  }

Auth: uses local `claude` CLI under the user's subscription (same as
auto_tag.py). ANTHROPIC_API_KEY is stripped from the subprocess env.

Usage:
  python3 build_canonical_map.py                     # default top-800 each
  python3 build_canonical_map.py --top 1000          # custom size
  python3 build_canonical_map.py --lang en           # one language only
  python3 build_canonical_map.py --dry-run           # print prompt, don't call
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LIBRARY = Path(
    os.path.expanduser("~/Eagle/MyLibrary.library")  # override via EAGLE_LIBRARY
)
OUTPUT_PATH = SCRIPT_DIR / ".tag_canonical_map.json"
CLAUDE_BIN_FALLBACK = "claude"  # last resort; prefer CLAUDE_BIN or `claude` on PATH
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or CLAUDE_BIN_FALLBACK
)


HANGUL_RE = re.compile(r"[가-힣]")


SYSTEM_PROMPT = """You are organizing a tag vocabulary for an image library.
Your job is to detect synonyms, variant spellings, and over-specific sub-forms,
and collapse them into a CANONICAL tag.

Rules:
- Each input tag must appear as a key in the output map.
- If a tag is already the best representative for its meaning, map it to ITSELF.
- If a tag is a synonym/variant/sub-form of a more general tag in the input,
  map it to that more general one. PREFER higher-frequency tags as canonical.
- Preserve meaningful distinctions. Do NOT over-merge:
  * "moody" ≠ "moodboard" (different meanings)
  * "minimal-makeup" ≠ "minimal" (the former is a makeup style)
  * "product-photography" ≠ "product-design"
- Case-only variants must collapse to lowercase:
  * "Y2K" → "y2k", "CGI" → "cgi", "UI" → "ui"
  * "3D렌더링" → "3d렌더링" (English embedded in Korean: lowercase the English part)
- Hyphenated compounds: if the compound only adds a redundant modifier present
  in many tags, prefer collapsing to the core (e.g. "minimal-aesthetic" → "minimal").
  But if the compound represents a distinct concept, keep it.
- Long-tail compounds (frequency 1-2) often have a core meaning already
  represented as a canonical: prefer mapping to the core
  (e.g. "chunky-loafers" → "loafers", "light-blue-background" → "background"
  if "background" exists, or to "light-blue" if that exists).
- Plurals/forms: prefer the more frequent of {minimal, minimalist}, etc.
- For Korean: same logic. "미니멀" vs "미니멀한" — pick the more frequent
  as canonical and map the other to it. "깔끔한" vs "깨끗한" — same meaning,
  collapse to the more frequent.
- Output ONLY a single JSON object, no prose, no markdown fences.

Output schema:
  {"map": {"alias1": "canonical1", "alias2": "canonical2", ...}}
"""


USER_PROMPT_TMPL = """The following are {n} tags from a {lang} image-library
vocabulary, sorted by usage frequency. Format: "tag (frequency)".

Build a canonical map per the rules in the system prompt. Every tag below
must appear as a key in your output (self-mappings allowed and expected
for canonical tags).

Tags:
{tag_list}
"""


def scan_tags(library: Path) -> tuple[Counter, Counter]:
    """Return (en_counter, ko_counter). Tag is classified as Korean if it
    contains any hangul character; otherwise English."""
    en: Counter = Counter()
    ko: Counter = Counter()
    for meta in sorted(library.glob("images/*/metadata.json")):
        try:
            with open(meta, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("isDeleted"):
            continue
        for t in d.get("tags") or []:
            t = t.strip()
            if not t:
                continue
            (ko if HANGUL_RE.search(t) else en)[t] += 1
    return en, ko


def pick_top(counter: Counter, top: int, min_freq: int = 2) -> list[tuple[str, int]]:
    """Return up to `top` most-frequent tags with freq >= min_freq."""
    eligible = [(t, c) for t, c in counter.most_common() if c >= min_freq]
    return eligible[:top]


def format_tag_list(tags: list[tuple[str, int]]) -> str:
    return "\n".join(f"{t} ({c})" for t, c in tags)


def call_claude(system_prompt: str, user_prompt: str, model: str, timeout: int = 600) -> str:
    """Call `claude -p` and return the raw text result."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        user_prompt,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"CLI error: {str(out.get('result', ''))[:500]}")
    return out.get("result", "")


def parse_map(text: str) -> dict[str, str]:
    """Extract the {alias: canonical} dict from Claude's response."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in response: {text[:300]}")
    data = json.loads(m.group(0))
    raw = data.get("map") or data
    if not isinstance(raw, dict):
        raise ValueError(f"unexpected map shape: {type(raw)}")
    # Coerce all values to strings, strip whitespace
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if k and v}


def merge_with_self_mappings(tags: list[tuple[str, int]], partial_map: dict[str, str]) -> dict[str, str]:
    """Ensure every input tag has an entry. Missing entries become self-maps."""
    out: dict[str, str] = {}
    for tag, _ in tags:
        out[tag] = partial_map.get(tag, tag)
    return out


def count_changes(m: dict[str, str]) -> int:
    return sum(1 for k, v in m.items() if k != v)


CHUNK_USER_PROMPT_TMPL = """The following is the FULL {lang} tag vocabulary
from an image library (sorted by frequency). Format: "tag (frequency)".

FULL VOCABULARY (for context — to help you spot synonyms across the whole set):
{full_list}

YOUR TASK: For the {chunk_n} tags listed below ONLY, output a canonical map.
Each listed tag must appear as a key in your output (self-mappings expected
for canonicals). You may map to any tag in the FULL vocabulary above,
preferring higher-frequency canonicals.

TAGS TO MAP:
{chunk_list}
"""


_print_lock = threading.Lock()


def _process_chunk_for_map(ci_total: tuple[int, int], lang: str, full_list: str,
                            chunk: list[tuple[str, int]], model: str,
                            timeout: int) -> dict[str, str]:
    """Worker for parallel chunk processing. Returns {tag: canonical} for
    this chunk, with self-mapping fallback on error."""
    ci, n_total = ci_total
    user_prompt = CHUNK_USER_PROMPT_TMPL.format(
        lang=lang.upper(), full_list=full_list,
        chunk_n=len(chunk), chunk_list=format_tag_list(chunk),
    )
    t0 = time.time()
    with _print_lock:
        print(f"  [chunk {ci}/{n_total}] {len(chunk)} tags...", flush=True)
    out: dict[str, str] = {}
    try:
        text = call_claude(SYSTEM_PROMPT, user_prompt, model, timeout=timeout)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        with _print_lock:
            print(f"    ✗ chunk {ci} failed: {type(e).__name__} — self-map", flush=True)
        return {t: t for t, _ in chunk}
    dt = time.time() - t0
    try:
        partial = parse_map(text)
    except ValueError as e:
        with _print_lock:
            print(f"    ✗ chunk {ci} parse failed ({e}) — self-map", flush=True)
        return {t: t for t, _ in chunk}
    chunk_keys = {t for t, _ in chunk}
    parsed = {k: v for k, v in partial.items() if k in chunk_keys}
    n_chg = sum(1 for k, v in parsed.items() if k != v)
    with _print_lock:
        print(f"    ✓ chunk {ci}: {len(parsed)}/{len(chunk)}, "
              f"{n_chg} changes, {dt:.1f}s", flush=True)
    for t, _ in chunk:
        out[t] = parsed.get(t, t)
    return out


def build_language(lang: str, tags: list[tuple[str, int]], model: str,
                   dry_run: bool, timeout: int, chunk_size: int = 80,
                   concurrency: int = 1) -> dict[str, str]:
    print(f"\n━━━ {lang.upper()} ━━━")
    print(f"  input tags: {len(tags)}  (top-freq: {tags[0]})")

    full_list = format_tag_list(tags)
    n_chunks = (len(tags) + chunk_size - 1) // chunk_size

    if dry_run:
        sample_chunk = tags[:min(chunk_size, len(tags))]
        sample_prompt = CHUNK_USER_PROMPT_TMPL.format(
            lang=lang.upper(), full_list=full_list,
            chunk_n=len(sample_chunk), chunk_list=format_tag_list(sample_chunk),
        )
        print(f"  would issue {n_chunks} chunks of ~{chunk_size} "
              f"(concurrency={concurrency})")
        print(f"  system prompt: {len(SYSTEM_PROMPT)} chars")
        print(f"  per-chunk user prompt: ~{len(sample_prompt)} chars")
        return {t: t for t, _ in tags}

    chunks: list[list[tuple[str, int]]] = []
    for start in range(0, len(tags), chunk_size):
        chunks.append(tags[start:start + chunk_size])

    full_map: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [
            ex.submit(_process_chunk_for_map, (ci + 1, n_chunks), lang,
                      full_list, chunk, model, timeout)
            for ci, chunk in enumerate(chunks)
        ]
        for fut in cf.as_completed(futures):
            try:
                full_map.update(fut.result())
            except Exception as e:  # noqa: BLE001
                with _print_lock:
                    print(f"    ✗ worker crash: {e}", flush=True)

    n_changes = count_changes(full_map)
    print(f"  total: {len(full_map)} mappings, {n_changes} changes "
          f"({n_changes / max(1, len(full_map)) * 100:.1f}%)")
    return full_map


def main() -> int:
    p = argparse.ArgumentParser(description="Build canonical tag map")
    p.add_argument("--library", default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)))
    p.add_argument("--top", type=int, default=800, help="Top N tags per language (default 800)")
    p.add_argument("--min-freq", type=int, default=2, help="Minimum frequency to include (default 2)")
    p.add_argument("--lang", choices=["en", "ko", "both"], default="both")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--timeout", type=int, default=180,
                   help="Per-chunk Claude call timeout in seconds (default 180)")
    p.add_argument("--chunk-size", type=int, default=80,
                   help="Tags per chunk (default 80)")
    p.add_argument("--concurrency", type=int, default=1,
                   help="Parallel chunk workers (default 1)")
    p.add_argument("--offset", type=int, default=0,
                   help="Skip first N tags within the top set (for chunk retry)")
    p.add_argument("--limit-tags", type=int, default=0,
                   help="Process only the first N tags after offset (0=all)")
    p.add_argument("--merge", action="store_true",
                   help="Merge into existing output file instead of overwriting")
    p.add_argument("--dry-run", action="store_true", help="Print prompts, do not call Claude")
    p.add_argument("--output", default=str(OUTPUT_PATH))
    args = p.parse_args()

    library = Path(args.library)
    if not library.exists():
        print(f"❌ library not found: {library}", file=sys.stderr)
        return 1

    if not args.dry_run and not Path(CLAUDE_BIN).exists():
        print(f"❌ claude CLI not found: {CLAUDE_BIN}", file=sys.stderr)
        return 1

    print(f"📚 Library: {library.name}")
    print(f"🤖 Model: {args.model}")
    print(f"🔢 Top per language: {args.top} (min freq {args.min_freq})")

    en_counter, ko_counter = scan_tags(library)
    print(f"\n📊 Scanned: en={len(en_counter)} unique, ko={len(ko_counter)} unique")

    en_top = pick_top(en_counter, args.top, args.min_freq)
    ko_top = pick_top(ko_counter, args.top, args.min_freq)

    def slice_for_run(top_list: list[tuple[str, int]]) -> list[tuple[str, int]]:
        start = args.offset
        end = start + args.limit_tags if args.limit_tags else len(top_list)
        return top_list[start:end]

    en_run = slice_for_run(en_top)
    ko_run = slice_for_run(ko_top)
    if args.offset or args.limit_tags:
        print(f"   slice: offset={args.offset}, limit={args.limit_tags or 'all'} "
              f"(en={len(en_run)}, ko={len(ko_run)})")

    en_map: dict[str, str] = {}
    ko_map: dict[str, str] = {}

    if args.lang in ("en", "both"):
        en_map = build_language("en", en_run, args.model, args.dry_run,
                                args.timeout, args.chunk_size, args.concurrency)
    if args.lang in ("ko", "both"):
        ko_map = build_language("ko", ko_run, args.model, args.dry_run,
                                args.timeout, args.chunk_size, args.concurrency)

    out_path = Path(args.output)

    if args.dry_run:
        print(f"\n(dry-run; no changes written to {out_path})")
        return 0

    if args.merge and out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            prior = json.load(f)
        prior_en = prior.get("map", {}).get("en", {}) or {}
        prior_ko = prior.get("map", {}).get("ko", {}) or {}
        # New mappings overlay prior; non-self-map values from this run win
        for k, v in en_map.items():
            if k != v or k not in prior_en:
                prior_en[k] = v
        for k, v in ko_map.items():
            if k != v or k not in prior_ko:
                prior_ko[k] = v
        en_map_final, ko_map_final = prior_en, prior_ko
        print(f"\n🔀 merged into existing file (kept prior mappings where this run was self-map)")
    else:
        en_map_final, ko_map_final = en_map, ko_map

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "library": library.name,
        "params": {"top": args.top, "min_freq": args.min_freq, "model": args.model,
                   "offset": args.offset, "limit_tags": args.limit_tags,
                   "chunk_size": args.chunk_size, "merged": args.merge},
        "stats": {
            "en_total": len(en_map_final),
            "ko_total": len(ko_map_final),
            "en_changes": count_changes(en_map_final),
            "ko_changes": count_changes(ko_map_final),
        },
        "map": {"en": en_map_final, "ko": ko_map_final},
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ saved: {out_path}")
    print(f"   en: {len(en_map_final)} mappings, {count_changes(en_map_final)} changes")
    print(f"   ko: {len(ko_map_final)} mappings, {count_changes(ko_map_final)} changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
