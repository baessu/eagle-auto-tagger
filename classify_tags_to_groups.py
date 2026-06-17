#!/usr/bin/env python3
"""Classify Eagle library tags into the user's 9 tag groups.

Reads the live library metadata.json for the group definitions, scans
metadata.json files for tag frequencies, and asks Claude to bucket each
high-frequency tag into one of the 9 groups.

Output: .tag_group_classification.json (used by apply_tag_groups.py)

Usage:
  python3 classify_tags_to_groups.py                      # default (freq >= 5)
  python3 classify_tags_to_groups.py --min-freq 3         # include more tags
  python3 classify_tags_to_groups.py --lang en            # english only
  python3 classify_tags_to_groups.py --dry-run            # preview prompts
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
OUTPUT_PATH = SCRIPT_DIR / ".tag_group_classification.json"
CLAUDE_BIN_FALLBACK = "claude"  # last resort; prefer CLAUDE_BIN or `claude` on PATH
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or CLAUDE_BIN_FALLBACK
)

HANGUL_RE = re.compile(r"[가-힣]")


GROUP_GUIDE = """GROUP DEFINITIONS — choose ONE group per tag:

1. Mood — 무드/감정. 이미지를 보고 느끼는 분위기·감정·정서.
   예: moody, melancholic, serene, playful, dreamy, intimate, edgy,
       contemplative, ethereal, nostalgic, energetic, romantic, brooding,
       차분한, 절제된, 몽환적인, 발랄한, 아련한, 강렬한

2. Visual Treatment — 비주얼 톤. 컬러 팔레트 + 라이팅 + 콘트라스트의
   결합 인상. 사진 톤·후가공·전반적 시각 처리.
   예: monochrome, pastel, earth-tones, warm-toned, cool-toned,
       high-contrast, soft-lit, sun-drenched, natural-light, studio-lighting,
       흑백, 파스텔톤, 자연광, 따뜻한톤

3. Style/Genre — 양식·시대성·문화권. 디자인 컨텍스트로서의 스타일 라벨.
   예: minimalist, brutalist, retro, y2k, mid-century, swiss-style,
       japanese-design, korean-design, parisian, scandinavian, wabi-sabi,
       미니멀한, 모던한, 레트로, 한국디자인

4. Medium — 매체·렌더링. 사진 vs 일러스트 vs 3D vs 페인팅 등 표현 방식.
   예: photography, illustration, 3d-render, cgi, hand-drawn, line-art,
       watercolor, vector-art, digital-art, mixed-media, painterly, film-grain,
       사진, 일러스트, 3d렌더링, 손그림

5. Composition — 구도·프레이밍·카메라 워크.
   예: portrait, close-up, full-body, flat-lay, overhead-shot, low-angle,
       profile-shot, studio-shot, candid, mirror-selfie, negative-space,
       symmetrical, side-view, top-view, 클로즈업, 인물사진, 풀샷

6. Use Case — 사용처·포맷. 이미지가 만들어진/쓰일 컨텍스트.
   예: editorial, lookbook, campaign, packaging-design, brand-identity,
       logo-design, advertising, magazine-cover, poster-design, ui-design,
       infographic, book-design, 패션에디토리얼, 룩북, 광고

7. Wearables — 사람이 입는 객체. 옷·신발·가방·선글라스 등 패션 아이템.
   예: knitwear, denim, leather-jacket, trench-coat, turtleneck, blazer,
       midi-skirt, hoodie, sneakers, knee-high-boots, loafers, sunglasses,
       handbag, tote-bag, pearl-necklace, baseball-cap, scarf,
       니트웨어, 트렌치코트, 선글라스, 가방

8. Props/Objects — 사람과 별개 물건·요소·환경. 패키지·소품·자연·공간·운송수단.
   예: tube-packaging, pump-bottle, glass-bottle, dropper-bottle, gift-box,
       perfume-bottle, wine-bottle, coffee, matcha, flower, plant, brick-wall,
       mirror, cafe-interior, storefront, scooter, bike, car,
       유리병, 튜브용기, 향수병, 꽃, 카페

9. Texture/Material — 표면감·소재·질감.
   예: glass-skin, dewy, matte, glossy, tactile, sculptural, translucent,
       leather, knit-texture, ceramics, kraft-paper, embossed, paper-texture,
       cream-texture, 매끈한, 광택, 촉각적인, 가죽, 종이질감

If a tag fits NONE of the above (too generic, too abstract, or unclear),
return "ungrouped".

Rules:
- Pick the SINGLE best-fitting group.
- If a tag could go in multiple, choose the one a visual director would
  reach for FIRST when filtering the library.
- Prefer Wearables for clothing/accessories; Props for non-wearable objects.
- "studio-photography" → Composition (where it's shot), not Medium.
- "fashion-photography" → Use Case (it's an output category).
- "editorial-illustration" → Use Case + Medium overlap; choose Use Case.
- Texture/Material is for the visual/tactile surface impression, not
  clothing made of that material (e.g. "leather-jacket" → Wearables,
  but "leather" alone → Texture/Material).
"""

USER_PROMPT_TMPL = """Below is a chunk of {n} tags ({lang}) from a visual
reference library, with usage frequency in parentheses.

Classify each tag into exactly one of the 9 groups defined in the system
prompt (or "ungrouped" if nothing fits).

Output ONLY a JSON object: {{"tag": "Group Name", ...}}
Group Name must be EXACTLY one of:
  Mood | Visual Treatment | Style/Genre | Medium | Composition |
  Use Case | Wearables | Props/Objects | Texture/Material | ungrouped

Tags to classify:
{tag_list}
"""

VALID_GROUPS = {
    "Mood", "Visual Treatment", "Style/Genre", "Medium", "Composition",
    "Use Case", "Wearables", "Props/Objects", "Texture/Material", "ungrouped",
}


def scan_tags(library: Path, min_freq: int) -> tuple[list, list]:
    en: Counter = Counter()
    ko: Counter = Counter()
    for meta in library.glob("images/*/metadata.json"):
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
    en_top = [(t, c) for t, c in en.most_common() if c >= min_freq]
    ko_top = [(t, c) for t, c in ko.most_common() if c >= min_freq]
    return en_top, ko_top


def fmt_list(tags: list[tuple[str, int]]) -> str:
    return "\n".join(f"{t} ({c})" for t, c in tags)


def call_claude(system_prompt: str, user_prompt: str, model: str, timeout: int = 240) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        user_prompt,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"CLI error: {str(out.get('result', ''))[:500]}")
    return out.get("result", "")


def parse_classification(text: str) -> dict[str, str]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in response: {text[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("not a dict")
    return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}


def normalize_group_name(name: str) -> str:
    """Map model's casing/whitespace variants to canonical group names."""
    n = name.strip()
    # Try exact match first
    if n in VALID_GROUPS:
        return n
    # Try case-insensitive match
    lower_map = {g.lower(): g for g in VALID_GROUPS}
    if n.lower() in lower_map:
        return lower_map[n.lower()]
    return "ungrouped"


_print_lock = threading.Lock()

def _process_chunk(ci_total: tuple[int, int], lang: str,
                   chunk: list[tuple[str, int]], model: str,
                   timeout: int) -> dict[str, str]:
    ci, n_total = ci_total
    user_prompt = USER_PROMPT_TMPL.format(
        n=len(chunk), lang=lang.upper(), tag_list=fmt_list(chunk),
    )
    t0 = time.time()
    with _print_lock:
        print(f"  [chunk {ci}/{n_total}] {len(chunk)} tags...", flush=True)
    chunk_class: dict[str, str] = {}
    try:
        text = call_claude(GROUP_GUIDE, user_prompt, model, timeout=timeout)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        with _print_lock:
            print(f"    ✗ chunk {ci} failed: {type(e).__name__} — ungrouped")
        for t, _ in chunk:
            chunk_class[t] = "ungrouped"
        return chunk_class
    dt = time.time() - t0
    try:
        partial = parse_classification(text)
    except ValueError as e:
        with _print_lock:
            print(f"    ✗ chunk {ci} parse failed ({e}) — ungrouped")
        for t, _ in chunk:
            chunk_class[t] = "ungrouped"
        return chunk_class
    chunk_keys = {t for t, _ in chunk}
    parsed = {k: normalize_group_name(v) for k, v in partial.items() if k in chunk_keys}
    dist = Counter(parsed.values())
    top_dist = ", ".join(f"{g}={n}" for g, n in dist.most_common(3))
    with _print_lock:
        print(f"    ✓ chunk {ci}: {len(parsed)}/{len(chunk)} top: {top_dist}, {dt:.1f}s",
              flush=True)
    for t, _ in chunk:
        chunk_class[t] = parsed.get(t, "ungrouped")
    return chunk_class


def classify_chunked(lang: str, tags: list[tuple[str, int]], model: str,
                     chunk_size: int, timeout: int, dry_run: bool,
                     concurrency: int = 1) -> dict[str, str]:
    print(f"\n━━━ {lang.upper()} — {len(tags)} tags ━━━")
    if not tags:
        return {}
    n_chunks = (len(tags) + chunk_size - 1) // chunk_size

    if dry_run:
        sample = tags[:min(chunk_size, len(tags))]
        prompt = USER_PROMPT_TMPL.format(
            n=len(sample), lang=lang.upper(), tag_list=fmt_list(sample))
        print(f"  would issue {n_chunks} chunks of ~{chunk_size} (concurrency={concurrency})")
        print(f"  system prompt: {len(GROUP_GUIDE)} chars")
        print(f"  sample user prompt: {len(prompt)} chars")
        return {t: "ungrouped" for t, _ in tags}

    chunks: list[list[tuple[str, int]]] = []
    for start in range(0, len(tags), chunk_size):
        chunks.append(tags[start:start + chunk_size])

    classification: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [
            ex.submit(_process_chunk, (ci + 1, n_chunks), lang, chunk, model, timeout)
            for ci, chunk in enumerate(chunks)
        ]
        for fut in cf.as_completed(futures):
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001
                with _print_lock:
                    print(f"    ✗ worker crash: {e}")
                continue
            classification.update(result)
    return classification


def main() -> int:
    p = argparse.ArgumentParser(description="Classify tags into Eagle tag groups")
    p.add_argument("--library", default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)))
    p.add_argument("--min-freq", type=int, default=5)
    p.add_argument("--lang", choices=["en", "ko", "both"], default="both")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--chunk-size", type=int, default=80)
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--concurrency", type=int, default=1,
                   help="Parallel chunk workers (default 1)")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip tags already classified in --output file")
    p.add_argument("--merge", action="store_true",
                   help="Merge new classifications into existing output file")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output", default=str(OUTPUT_PATH))
    args = p.parse_args()

    library = Path(args.library)
    if not library.exists():
        print(f"❌ library not found: {library}", file=sys.stderr)
        return 1

    # Read group definitions from the live library
    with open(library / "metadata.json", encoding="utf-8") as f:
        meta = json.load(f)
    groups = meta.get("tagsGroups", []) or []
    print(f"📚 Library: {library.name}")
    print(f"📑 Groups: {len(groups)}")
    for g in groups:
        print(f"   • {g.get('name')} (id={g.get('id')})")

    en_tags, ko_tags = scan_tags(library, args.min_freq)
    print(f"\n📊 Eligible tags (freq ≥ {args.min_freq}): en={len(en_tags)}, ko={len(ko_tags)}")

    # Load existing classification if skipping
    existing: dict[str, dict[str, str]] = {"en": {}, "ko": {}}
    output_path = Path(args.output)
    if (args.skip_existing or args.merge) and output_path.exists():
        with open(output_path, encoding="utf-8") as f:
            prior = json.load(f)
        existing = prior.get("classification", {"en": {}, "ko": {}})
        print(f"   prior classification: en={len(existing.get('en', {}))}, "
              f"ko={len(existing.get('ko', {}))} already mapped")

    if args.skip_existing:
        en_known = set(existing.get("en", {}))
        ko_known = set(existing.get("ko", {}))
        en_tags = [(t, c) for t, c in en_tags if t not in en_known]
        ko_tags = [(t, c) for t, c in ko_tags if t not in ko_known]
        print(f"   after skip-existing: en={len(en_tags)}, ko={len(ko_tags)} new")

    en_class: dict[str, str] = {}
    ko_class: dict[str, str] = {}
    if args.lang in ("en", "both"):
        en_class = classify_chunked("en", en_tags, args.model,
                                     args.chunk_size, args.timeout, args.dry_run,
                                     concurrency=args.concurrency)
    if args.lang in ("ko", "both"):
        ko_class = classify_chunked("ko", ko_tags, args.model,
                                     args.chunk_size, args.timeout, args.dry_run,
                                     concurrency=args.concurrency)

    if args.merge:
        merged_en = dict(existing.get("en", {}))
        merged_en.update(en_class)
        merged_ko = dict(existing.get("ko", {}))
        merged_ko.update(ko_class)
        en_class, ko_class = merged_en, merged_ko
        print(f"\n🔀 merged with existing")

    # Aggregate stats
    all_class = {**en_class, **ko_class}
    dist = Counter(all_class.values())
    print("\n=== Distribution ===")
    for g, n in dist.most_common():
        print(f"  {g:>22}  {n}")

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "library": library.name,
        "params": {"min_freq": args.min_freq, "chunk_size": args.chunk_size,
                   "model": args.model},
        "groups": [{"id": g["id"], "name": g["name"]} for g in groups],
        "classification": {"en": en_class, "ko": ko_class},
        "distribution": dict(dist),
    }
    if args.dry_run:
        print(f"\n(dry-run; no changes written to {args.output})")
        return 0

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✅ saved: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
