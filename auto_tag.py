#!/usr/bin/env python3
"""Eagle Auto-Tagger

Scans the local Eagle library for untagged images, asks Claude (vision) to
produce bilingual tags (20 EN + 20 KR) per image, and writes them back via
Eagle's HTTP API.

Auth: uses the local `claude` CLI (Claude Code subscription / OAuth). No
ANTHROPIC_API_KEY required — in fact the key is stripped from the subprocess
env so the CLI uses subscription auth instead of API billing.

Usage:
  python3 auto_tag.py --dry-run 3              # analyze 3 samples, print only
  python3 auto_tag.py --limit 10               # process first 10 untagged
  python3 auto_tag.py --concurrency 4          # full run, 4 parallel
  python3 auto_tag.py --resume                 # skip ids in progress log
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Callable

# --- Config -----------------------------------------------------------------

EAGLE_BASE = "http://localhost:41595"
CLAUDE_MODEL_DEFAULT = "sonnet"  # alias resolved by the CLI to the latest Sonnet
CLAUDE_MODEL_OPUS = "opus"
CLAUDE_BIN_FALLBACK = "claude"  # last resort; prefer CLAUDE_BIN or `claude` on PATH
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or CLAUDE_BIN_FALLBACK
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_LOG = SCRIPT_DIR / ".tag_progress.jsonl"
FAILED_LOG = SCRIPT_DIR / ".tag_failed.jsonl"
PROMPT_PROGRESS_LOG = SCRIPT_DIR / ".prompt_progress.jsonl"
PROMPT_FAILED_LOG = SCRIPT_DIR / ".prompt_failed.jsonl"
TAG_DICT_CACHE = SCRIPT_DIR / ".tag_dictionary_cache.json"
TAG_DICT_TTL_SECONDS = 6 * 3600  # 6 hours
TAG_DICT_TOP_N = 400  # tags per language in the system prompt vocabulary
TAG_DICT_MIN_FREQ = 2

# Map Eagle folder ID -> prompt category. Items in any of these folders
# receive a generative-AI prompt in the annotation field instead of tags;
# the category selects which system prompt to use.
#
#   EAGLE_PROMPT_FOLDERS_PHOTO         -> "photo"        (photographic refs)
#   EAGLE_PROMPT_FOLDERS_ILLUSTRATION  -> "illustration" (artwork, anime, etc.)
#   EAGLE_PROMPT_FOLDERS               -> legacy alias, treated as "photo"
PROMPT_CATEGORY_ENVS: list[tuple[str, str]] = [
    ("photo", "EAGLE_PROMPT_FOLDERS_PHOTO"),
    ("illustration", "EAGLE_PROMPT_FOLDERS_ILLUSTRATION"),
    ("photo", "EAGLE_PROMPT_FOLDERS"),  # legacy fallback
]
PROMPT_FOLDER_CATEGORIES: dict[str, str] = {}
for _cat, _env in PROMPT_CATEGORY_ENVS:
    for _fid in os.environ.get(_env, "").split(","):
        _fid = _fid.strip()
        if _fid and _fid not in PROMPT_FOLDER_CATEGORIES:
            PROMPT_FOLDER_CATEGORIES[_fid] = _cat
PROMPT_FOLDERS: set[str] = set(PROMPT_FOLDER_CATEGORIES)

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "heic", "heif", "avif"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4MB soft cap before resize
MAX_IMAGE_DIM = 1568  # Claude's recommended edge length

SYSTEM_PROMPT_BASE = """당신은 이미지를 보고 디자인 자료용 태그를 생성하는 전문가입니다.
Eagle 라이브러리 검색·큐레이션을 돕기 위해 형용사(감상) + 카테고리(키워드) 태그를 만듭니다.

규칙:
- 영문 태그 20개: 감상 형용사 10개 + 카테고리/키워드 10개
- 한글 태그 20개: 감상 형용사 10개 + 카테고리/키워드 10개
- 영문은 lowercase-kebab-case, 합성어만 하이픈으로 연결 (예: fashion-editorial, 3d-render)
- 한글은 띄어쓰기 없이 붙여쓰기 (예: 제품디자인, 그래픽디자인)
- 형용사는 이미지를 보고 즉각 떠오르는 감각·감정을 쓴다 (예: moody, 몽환적인)
- 카테고리는 장르/매체/소재/스타일/컬러 축에서 실제 검색에 쓸 단어를 쓴다
- 이미지 안의 텍스트나 워터마크는 태그로 만들지 않는다
- 영문과 한글은 1:1 번역이 아니어도 된다 (각 언어에서 자연스러운 표현 우선)
- 중복 금지, 너무 추상적인 단어(beautiful, nice, 멋진) 금지

태그 사전 활용 (가장 중요):
- 아래 PREFERRED VOCABULARY에서 의미가 맞는 태그가 있으면 **반드시** 그것을 우선 사용하세요
- 같은 의미를 다른 표기로 쓰지 마세요 (예: 사전에 "minimalist"가 있으면 "minimal"·"minimalist-aesthetic"·"clean-minimal" 등 변형 금지)
- 사전에 적절한 태그가 없을 때만 새 태그를 만드세요
- 새 태그는 사전 단어들과 어울리는 표기 규칙(영문 kebab-case, 한글 붙여쓰기)을 따르세요

출력: JSON만. 설명·마크다운 펜스 없이 아래 스키마 그대로.
{"en": ["tag1", ..., "tag20"], "ko": ["태그1", ..., "태그20"]}
"""

SYSTEM_PROMPT_VOCAB_TEMPLATE = """

PREFERRED VOCABULARY — Use these existing tags whenever they capture the
intended meaning. Frequency in parentheses shows usage count across the
library. Higher frequency = stronger preference for that exact spelling.

The vocabulary is organized by TAG GROUP. Each group represents a
visual-curation axis. When tagging an image, think across these axes
and pick tags from groups that genuinely apply to this image —
DO NOT force-fill every group. A still-life product shot has no
Wearables; a flat character illustration has no Composition camera
work. Coverage of 4-7 relevant groups per image is typical.

If you must introduce a new tag (no existing one fits), give it a
spelling that matches the conventions of its target group, and pick
the group it would naturally join.

EN — {n_en} tags by group:
{en_by_group}

KO — {n_ko} tags by group:
{ko_by_group}
"""

# Module-level cache so a single process doesn't rebuild repeatedly
_SYSTEM_PROMPT_CACHE: tuple[float, str] | None = None
SYSTEM_PROMPT = SYSTEM_PROMPT_BASE  # fallback if dictionary build fails

# Set by main() so worker functions can build the vocab-enriched prompt.
_LIBRARY_PATH: Path | None = None

USER_PROMPT = "이 이미지를 분석해 규칙에 맞는 태그 40개를 생성하세요. PREFERRED VOCABULARY에 의미가 맞는 태그가 있으면 그걸 우선 사용하세요."


# --- Tag dictionary (canonical vocabulary for the system prompt) -----------

HANGUL_RE = re.compile(r"[가-힣]")


def _load_tag_to_group(library_path: Path) -> tuple[dict[str, str], list[str]]:
    """Read library's metadata.json tagsGroups and return (tag→group_name, group_order)."""
    try:
        with open(library_path / "metadata.json", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}, []
    tag_to_group: dict[str, str] = {}
    group_order: list[str] = []
    for g in meta.get("tagsGroups") or []:
        name = g.get("name", "")
        if not name:
            continue
        group_order.append(name)
        for t in g.get("tags") or []:
            tag_to_group[t] = name
    return tag_to_group, group_order


def build_tag_dictionary(library_path: Path, top_n: int = TAG_DICT_TOP_N,
                         min_freq: int = TAG_DICT_MIN_FREQ) -> dict:
    """Scan metadata.json on disk, count tag usage, return top-N per language
    along with each tag's group membership."""
    from collections import Counter as _Counter
    en: _Counter = _Counter()
    ko: _Counter = _Counter()
    for meta in library_path.glob("images/*/metadata.json"):
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
    en_top = [[t, c] for t, c in en.most_common() if c >= min_freq][:top_n]
    ko_top = [[t, c] for t, c in ko.most_common() if c >= min_freq][:top_n]
    tag_to_group, group_order = _load_tag_to_group(library_path)
    return {
        "built_at": time.time(),
        "library": library_path.name,
        "en": en_top,
        "ko": ko_top,
        "tag_to_group": tag_to_group,
        "group_order": group_order,
    }


def load_tag_dictionary(library_path: Path, force_rebuild: bool = False) -> dict:
    """Return tag dictionary, rebuilding if cache is missing or expired."""
    if not force_rebuild and TAG_DICT_CACHE.exists():
        try:
            with open(TAG_DICT_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            age = time.time() - cached.get("built_at", 0)
            if age < TAG_DICT_TTL_SECONDS and cached.get("library") == library_path.name:
                return cached
        except (OSError, json.JSONDecodeError):
            pass
    d = build_tag_dictionary(library_path)
    try:
        with open(TAG_DICT_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return d


def _format_by_group(pairs: list, tag_to_group: dict[str, str],
                     group_order: list[str]) -> str:
    """Group pairs by their tag group, render as multi-line block.
    Tags with no group go under '[ungrouped]'."""
    from collections import defaultdict as _dd
    buckets: dict[str, list] = _dd(list)
    for t, c in pairs:
        g = tag_to_group.get(t, "ungrouped")
        buckets[g].append((t, c))
    # group_order + any extras that appeared (e.g. ungrouped, or new groups)
    seen = set()
    ordered_groups: list[str] = []
    for g in group_order:
        if g in buckets:
            ordered_groups.append(g)
            seen.add(g)
    for g in buckets:
        if g not in seen and g != "ungrouped":
            ordered_groups.append(g)
            seen.add(g)
    if "ungrouped" in buckets:
        ordered_groups.append("ungrouped")
    lines = []
    for g in ordered_groups:
        items = sorted(buckets[g], key=lambda x: -x[1])
        if not items:
            continue
        rendered = ", ".join(f"{t} ({c})" for t, c in items)
        lines.append(f"  [{g}] {rendered}")
    return "\n".join(lines)


def format_vocab_section(tag_dict: dict) -> str:
    """Format the dictionary into the PREFERRED VOCABULARY section, with
    each language's tags grouped by tag-group axis."""
    tag_to_group = tag_dict.get("tag_to_group") or {}
    group_order = tag_dict.get("group_order") or []
    return SYSTEM_PROMPT_VOCAB_TEMPLATE.format(
        n_en=len(tag_dict["en"]),
        n_ko=len(tag_dict["ko"]),
        en_by_group=_format_by_group(tag_dict["en"], tag_to_group, group_order),
        ko_by_group=_format_by_group(tag_dict["ko"], tag_to_group, group_order),
    )


def get_system_prompt(library_path: Path) -> str:
    """Return the full system prompt with PREFERRED VOCABULARY embedded.
    Cached per-process; falls back to base prompt if anything fails."""
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is not None:
        ts, prompt = _SYSTEM_PROMPT_CACHE
        if time.time() - ts < TAG_DICT_TTL_SECONDS:
            return prompt
    try:
        d = load_tag_dictionary(library_path)
        prompt = SYSTEM_PROMPT_BASE + format_vocab_section(d)
        _SYSTEM_PROMPT_CACHE = (time.time(), prompt)
        return prompt
    except Exception:  # noqa: BLE001 — defensive; never block tagging on dict build
        return SYSTEM_PROMPT_BASE

PROMPT_SYSTEM_PROMPT_PHOTO = """You are an elite visual reverse-engineering analyst specialized in reconstructing highly accurate generative AI prompts from reference images.

Your task is NOT to simply describe the image.

Your goal is to analyze the image like a professional cinematographer, commercial photographer, art director, and generative AI prompt engineer combined.

Analyze the image in extreme detail so that another image generation model could recreate a visually and stylistically similar result as accurately as possible.

Focus on the underlying visual construction of the image.

Avoid generic descriptions.
Avoid subjective interpretation unless it affects the visual outcome.
Avoid storytelling unless it is visually represented.

Break the analysis into the following layers:

1. SUBJECT LAYER
- Main subject(s)
- Physical appearance
- Pose and gesture
- Clothing/materials
- Hair/makeup
- Product design details
- Shape language
- Important visual identifiers

2. COMPOSITION LAYER
- Camera angle
- Framing
- Crop type
- Subject placement
- Perspective
- Lens characteristics
- Depth of field
- Foreground/background relationships
- Symmetry/asymmetry
- Visual hierarchy
- Negative space usage

3. LIGHTING LAYER
- Lighting direction
- Light source type
- Hard vs soft light
- Contrast level
- Shadow behavior
- Highlight behavior
- Specular reflections
- Ambient lighting
- Color temperature
- Cinematic or studio lighting characteristics

4. MATERIAL & TEXTURE LAYER
- Surface materials
- Skin texture
- Glossiness/matte balance
- Transparency/translucency
- Liquid behavior
- Reflection/refraction
- Microtextures
- Environmental texture details

5. COLOR SYSTEM LAYER
- Dominant colors
- Secondary colors
- Accent colors
- Gradient structures
- Tonal relationships
- Saturation level
- Overall palette mood
- Exact hex colors if inferable

6. BACKGROUND & ENVIRONMENT LAYER
- Environment type
- Background construction
- Spatial depth
- Props and supporting elements
- Environmental storytelling through visuals only

7. STYLE & RENDERING LAYER
- Photography style
- Commercial/artistic references
- Fashion/editorial influences
- Advertising aesthetic
- CGI vs photoreal balance
- Rendering characteristics
- Post-processing style
- Retouching characteristics

8. MOOD & BRAND IMPRESSION
- Emotional tone
- Brand positioning impression
- Luxury/minimal/clinical/editorial/etc.
- Psychological feeling created by the image

9. GENERATIVE AI RECONSTRUCTION
Based on all observations above, generate:

A. A highly optimized professional image-generation prompt in fluent English.

B. A concise "core prompt" version optimized for models like Midjourney, Flux, SDXL, or GPT Image.

C. A negative prompt section describing what should NOT appear.

D. Optional model-specific optimization notes if relevant.

The final prompts should prioritize:
- visual fidelity
- compositional accuracy
- lighting realism
- material realism
- generation consistency

Do not output JSON unless explicitly requested.
Use structured professional formatting.
Assume the target audience is an advanced AI image creator.
"""

PROMPT_SYSTEM_PROMPT_ILLUSTRATION = """You are an elite illustration reverse-engineering analyst specialized in reconstructing highly accurate generative AI prompts from reference artwork, illustrations, anime frames, concept art, comics, paintings, and stylized digital art.

Your task is NOT to simply describe the image.

Your goal is to analyze the underlying visual language, stylistic system, rendering logic, and artistic construction of the artwork so that another image generation model could recreate a highly similar result.

Analyze the image like a master illustrator, animation art director, concept artist, and AI prompt engineer combined.

Focus on:
- stylization logic
- shape language
- rendering style
- artistic abstraction
- visual design systems
- illustration-specific techniques

Avoid generic descriptions.
Avoid storytelling unless visually represented.
Avoid focusing on realism unless realism is part of the style itself.

Break the analysis into the following layers:

1. SUBJECT & CHARACTER DESIGN LAYER
- Main subjects or characters
- Character archetype
- Silhouette design
- Body proportions
- Facial stylization
- Clothing design language
- Accessories and props
- Hairstyle structure
- Visual motifs
- Important recognizable traits

2. SHAPE LANGUAGE LAYER
- Rounded vs angular forms
- Geometric vs organic construction
- Exaggeration level
- Dynamic vs static design
- Anatomy stylization
- Simplification logic
- Visual rhythm
- Large-medium-small shape balance

3. LINEWORK ANALYSIS LAYER
- Line thickness
- Variable line weight
- Ink style
- Clean vs sketchy lines
- Vector-like vs hand-drawn appearance
- Outline emphasis
- Edge handling
- Manga/anime/comic line characteristics

4. COLOR & LIGHTING SYSTEM LAYER
- Dominant palette
- Accent colors
- Saturation strategy
- Cel shading vs soft rendering
- Gradient usage
- Rim lighting
- Atmospheric lighting
- Stylized lighting logic
- Color harmony relationships
- Exact hex colors if inferable

5. RENDERING & PAINTING TECHNIQUE LAYER
- Cel shading
- Painterly rendering
- Watercolor simulation
- Airbrush rendering
- Flat illustration style
- Semi-realistic rendering
- Brush texture characteristics
- Texture overlays
- Rendering complexity level
- Surface simplification strategy

6. COMPOSITION & CINEMATIC LAYER
- Camera angle
- Framing
- Perspective distortion
- Dynamic composition
- Manga panel energy
- Cinematic staging
- Negative space
- Visual hierarchy
- Depth construction
- Focus guidance techniques

7. STYLE DNA & ARTISTIC INFLUENCE LAYER
- Anime influences
- Western illustration influences
- Game art influences
- Webtoon/manhwa characteristics
- Retro illustration references
- Editorial illustration references
- Concept art influences
- Specific aesthetic lineage
- Comparable artists/studios/styles if recognizable

8. ENVIRONMENT & WORLD DESIGN LAYER
- Background style
- Environmental simplification
- Architectural language
- Prop design consistency
- Atmosphere construction
- Environmental storytelling through visuals only

9. MOOD & EMOTIONAL IMPRESSION LAYER
- Emotional tone
- Energy level
- Psychological feeling
- Fantasy/sci-fi/drama/comedic mood
- Luxury/minimal/cute/dark/etc.
- Intended audience impression

10. GENERATIVE AI RECONSTRUCTION

Based on all observations above, generate:

A. A highly optimized professional illustration-generation prompt in fluent English.

B. A concise "core prompt" version optimized for models like Midjourney, Niji Journey, Flux, SDXL, NovelAI, GPT Image, or anime-focused models.

C. A negative prompt section describing what should NOT appear.

D. Style tags and rendering tags.

E. Optional model-specific optimization notes if relevant.

The final prompts should prioritize:
- stylistic fidelity
- shape-language accuracy
- rendering consistency
- composition accuracy
- illustration aesthetic preservation

Do not output JSON unless explicitly requested.
Use structured professional formatting.
Assume the target audience is an advanced AI image creator and professional illustrator.
"""

PROMPT_SYSTEM_PROMPTS: dict[str, str] = {
    "photo": PROMPT_SYSTEM_PROMPT_PHOTO,
    "illustration": PROMPT_SYSTEM_PROMPT_ILLUSTRATION,
}

# --- Data types -------------------------------------------------------------


@dataclasses.dataclass
class Item:
    id: str
    name: str
    ext: str
    path: Path
    width: int | None
    height: int | None
    folders: list[str]
    has_tags: bool
    has_annotation: bool


@dataclasses.dataclass
class ItemResult:
    """Result of processing one item. Either or both sub-results may be
    populated; both being None means nothing was attempted (caller should
    not have asked). Per-step exceptions are captured so a tag failure
    doesn't prevent the prompt step from running."""
    id: str
    name: str
    elapsed: float
    model: str
    # Tag sub-step
    tags: list[str] | None = None
    tags_error: BaseException | None = None
    # Prompt sub-step
    annotation: str | None = None
    category: str | None = None
    prompt_error: BaseException | None = None

    @property
    def any_success(self) -> bool:
        return self.tags is not None or self.annotation is not None

    @property
    def any_error(self) -> bool:
        return self.tags_error is not None or self.prompt_error is not None


# --- Eagle API --------------------------------------------------------------


def eagle_get(path: str, **params: Any) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{EAGLE_BASE}{path}" + (f"?{qs}" if qs else "")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


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


def _item_from_meta(meta: Path) -> Item | None:
    """Build Item from a metadata.json file. Returns None if skippable."""
    try:
        with open(meta, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("isDeleted"):
        return None
    ext = (d.get("ext") or "").lower()
    if ext not in IMAGE_EXTS:
        return None
    folder = meta.parent
    # Prefer original (no "_thumbnail" suffix, no "_" prefix).
    all_imgs = [p for p in folder.iterdir() if p.suffix.lower().lstrip(".") == ext]
    originals = [p for p in all_imgs if not p.name.startswith("_") and "_thumbnail" not in p.name]
    if not originals:
        return None
    return Item(
        id=d["id"],
        name=d.get("name", ""),
        ext=ext,
        path=originals[0],
        width=d.get("width"),
        height=d.get("height"),
        folders=list(d.get("folders") or []),
        has_tags=bool(d.get("tags")),
        has_annotation=bool((d.get("annotation") or "").strip()),
    )


def item_mode(item: Item) -> str:
    """Return 'prompt' if the item belongs to any configured prompt folder,
    else 'tags'."""
    if PROMPT_FOLDERS and any(fid in PROMPT_FOLDERS for fid in item.folders):
        return "prompt"
    return "tags"


def item_prompt_category(item: Item) -> str:
    """Return the prompt category for a prompt-mode item ('photo' or
    'illustration'). If an item is in multiple prompt folders across
    categories, the first match in folder order wins."""
    for fid in item.folders:
        cat = PROMPT_FOLDER_CATEGORIES.get(fid)
        if cat:
            return cat
    return "photo"  # safe default, should not reach here in prompt mode


def needs_work(item: Item, force_annotation: bool = False) -> tuple[bool, bool]:
    """Return (needs_tags, needs_prompt) for this item.

    - All items get tags if they don't have any (regardless of folder).
    - Only prompt-folder items also get a prompt; existing annotations are
      preserved unless force_annotation is set.
    """
    needs_tags = not item.has_tags
    is_prompt_folder = item_mode(item) == "prompt"
    needs_prompt = is_prompt_folder and (not item.has_annotation or force_annotation)
    return needs_tags, needs_prompt


def list_pending(library_path: Path, mode_filter: str = "auto", force_annotation: bool = False) -> list[Item]:
    """Scan metadata.json on disk.

    mode_filter:
      - "auto":   include items needing either tags or prompt work
      - "tags":   include items needing tags (regardless of folder)
      - "prompt": include items needing prompt (prompt-folder items only)
    """
    items: list[Item] = []
    img_root = library_path / "images"
    for meta in sorted(img_root.glob("*/metadata.json")):
        it = _item_from_meta(meta)
        if not it:
            continue
        nt, np_ = needs_work(it, force_annotation=force_annotation)
        if mode_filter == "tags" and not nt:
            continue
        if mode_filter == "prompt" and not np_:
            continue
        if mode_filter == "auto" and not (nt or np_):
            continue
        items.append(it)
    return items


def find_item_by_id(library_path: Path, item_id: str) -> Item | None:
    meta = library_path / "images" / f"{item_id}.info" / "metadata.json"
    if not meta.exists():
        return None
    return _item_from_meta(meta)


def update_item_tags(item_id: str, tags: list[str]) -> dict:
    return eagle_post("/api/item/update", {"id": item_id, "tags": tags})


def update_item_annotation(item_id: str, annotation: str) -> dict:
    return eagle_post("/api/item/update", {"id": item_id, "annotation": annotation})


# --- Image prep -------------------------------------------------------------


NATIVE_FORMATS = {"jpg", "jpeg", "png", "gif", "webp"}


def prep_image(path: Path) -> tuple[Path, Callable[[], None]]:
    """Return (path_to_send, cleanup). Resize/convert if too large or non-native.

    The CLI's Read tool reads files directly, so we skip base64 entirely and
    pass either the original path (no cleanup needed) or a temp file (cleanup
    deletes it).
    """
    ext = path.suffix.lower().lstrip(".")

    if ext in NATIVE_FORMATS and path.stat().st_size <= MAX_IMAGE_BYTES:
        # Cheap dimension check; if PIL missing, send original as-is.
        try:
            from PIL import Image
            with Image.open(path) as im:
                if max(im.size) <= MAX_IMAGE_DIM:
                    return path, lambda: None
        except ImportError:
            return path, lambda: None

    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            f"Image too large or unsupported format ({ext}); install Pillow: uv pip install Pillow"
        ) from e

    with Image.open(path) as im:
        im = im.convert("RGB") if im.mode not in ("RGB", "RGBA", "L") else im
        im.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.Resampling.LANCZOS)
        suffix = ".png" if im.mode == "RGBA" else ".jpg"
        fd, tmp_name = tempfile.mkstemp(prefix="eagletag_", suffix=suffix)
        os.close(fd)
        if im.mode == "RGBA":
            im.save(tmp_name, format="PNG", optimize=True)
        else:
            im.save(tmp_name, format="JPEG", quality=88, optimize=True)

    tmp_path = Path(tmp_name)
    return tmp_path, lambda: tmp_path.unlink(missing_ok=True)


# --- Claude (via CLI / subscription) ---------------------------------------


def call_claude_cli(image_path: Path, model: str, system_prompt: str, user_prompt: str) -> dict:
    """Run `claude -p` over the user's subscription. Returns a content-block
    dict shaped like the Anthropic API response so the rest of the code is
    unchanged."""
    # Force OAuth auth — if ANTHROPIC_API_KEY is set the CLI would use the API.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [
        CLAUDE_BIN,
        "--tools", "Read",
        "--allowedTools", "Read",
        "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        f"Read the image at {image_path}. {user_prompt}",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"non-JSON CLI output: {proc.stdout[:300]}") from e
    if out.get("is_error"):
        raise RuntimeError(f"CLI error: {str(out.get('result', ''))[:300]}")
    return {"content": [{"type": "text", "text": out.get("result", "")}]}


def parse_tags(resp: dict) -> tuple[list[str], list[str]]:
    text = ""
    for block in resp.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    # Strip markdown fences if model added them
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Find first JSON object
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in response: {text[:200]}")
    data = json.loads(m.group(0))
    en = [t.strip() for t in data.get("en", []) if isinstance(t, str) and t.strip()]
    ko = [t.strip() for t in data.get("ko", []) if isinstance(t, str) and t.strip()]
    return en, ko


def parse_prompt(resp: dict) -> str:
    """Extract the free-form annotation text from a prompt-mode response."""
    text = ""
    for block in resp.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    return text.strip()


# --- Orchestration ----------------------------------------------------------


def load_progress() -> set[str]:
    done: set[str] = set()
    for log_path in (PROGRESS_LOG, PROMPT_PROGRESS_LOG):
        if not log_path.exists():
            continue
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                pass
    return done


def append_log(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _run_tags_step(item: Item, img_path: Path, model: str, dry_run: bool) -> list[str]:
    """Generate tags and write to Eagle. Returns the final tag list."""
    system_prompt = get_system_prompt(_LIBRARY_PATH) if _LIBRARY_PATH else SYSTEM_PROMPT_BASE
    resp = call_claude_cli(
        img_path, model,
        system_prompt=system_prompt,
        user_prompt="Return ONLY the JSON object described in the system prompt. No prose, no markdown fences.",
    )
    en, ko = parse_tags(resp)
    if len(en) < 15 or len(ko) < 15:
        raise ValueError(f"tag count too low: en={len(en)}, ko={len(ko)}")
    en = list(dict.fromkeys(en))[:20]
    ko = list(dict.fromkeys(ko))[:20]
    tags = en + ko
    if not dry_run:
        result = update_item_tags(item.id, tags)
        if result.get("status") != "success":
            raise RuntimeError(f"Eagle update failed: {result}")
        got = result.get("data", {}).get("tags", [])
        if set(got) != set(tags):
            diff_missing = set(tags) - set(got)
            diff_extra = set(got) - set(tags)
            raise RuntimeError(f"tag mismatch after write: missing={diff_missing}, extra={diff_extra}")
    return tags


def _run_prompt_step(item: Item, img_path: Path, model: str, dry_run: bool) -> tuple[str, str]:
    """Generate generative-AI prompt and write to annotation. Returns
    (annotation, category)."""
    category = item_prompt_category(item)
    system_prompt = PROMPT_SYSTEM_PROMPTS.get(category, PROMPT_SYSTEM_PROMPT_PHOTO)
    resp = call_claude_cli(
        img_path, model,
        system_prompt=system_prompt,
        user_prompt="Analyze this image following the layered protocol in the system prompt. Output structured prose only, no JSON.",
    )
    annotation = parse_prompt(resp)
    if len(annotation) < 200:
        raise ValueError(f"annotation too short: {len(annotation)} chars")
    if not dry_run:
        result = update_item_annotation(item.id, annotation)
        if result.get("status") != "success":
            raise RuntimeError(f"Eagle update failed: {result}")
        got = result.get("data", {}).get("annotation", "")
        if got.strip() != annotation.strip():
            raise RuntimeError(
                f"annotation mismatch after write: got {len(got)} chars, expected {len(annotation)}"
            )
    return annotation, category


def process_one(item: Item, model: str, dry_run: bool,
                do_tags: bool, do_prompt: bool) -> ItemResult:
    """Run any subset of {tags, prompt} steps for one item. Step failures
    are captured into ItemResult so the caller can log them independently."""
    t0 = time.time()
    out = ItemResult(id=item.id, name=item.name, elapsed=0.0, model=model)
    img_path, cleanup = prep_image(item.path)
    try:
        if do_tags:
            try:
                out.tags = _run_tags_step(item, img_path, model, dry_run)
            except Exception as e:  # noqa: BLE001
                out.tags_error = e
        if do_prompt:
            try:
                annotation, category = _run_prompt_step(item, img_path, model, dry_run)
                out.annotation = annotation
                out.category = category
            except Exception as e:  # noqa: BLE001
                out.prompt_error = e
    finally:
        cleanup()
    out.elapsed = time.time() - t0
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Eagle Auto-Tagger (Claude vision)")
    p.add_argument("--library", default=os.environ.get(
        "EAGLE_LIBRARY",
        os.path.expanduser("~/Eagle/MyLibrary.library"),  # override via EAGLE_LIBRARY
    ), help="Eagle library path (or set EAGLE_LIBRARY)")
    p.add_argument("--dry-run", type=int, default=0, metavar="N",
                   help="Analyze N samples, print tags only (no DB write)")
    p.add_argument("--limit", type=int, default=0, help="Process only first N untagged items (0=all)")
    p.add_argument("--concurrency", type=int, default=3, help="Parallel workers (default 3)")
    p.add_argument("--model", default=CLAUDE_MODEL_DEFAULT,
                   help=f"Claude model alias or full name (default: {CLAUDE_MODEL_DEFAULT})")
    p.add_argument("--opus", action="store_true", help=f"Use '{CLAUDE_MODEL_OPUS}' alias instead")
    p.add_argument("--resume", action="store_true", help="Skip ids already in progress log")
    p.add_argument("--retry-failed", action="store_true", help="Only process ids in failed log")
    p.add_argument("--item-id", default=None,
                   help="Process a single item by Eagle id (for watcher/hook mode)")
    p.add_argument("--mode", choices=["auto", "tags", "prompt"], default="auto",
                   help="auto: route by folder; tags|prompt: filter to one mode only")
    p.add_argument("--force-annotation", action="store_true",
                   help="In prompt mode, overwrite existing annotations")
    args = p.parse_args()

    if not Path(CLAUDE_BIN).exists():
        print(f"❌ claude CLI not found: {CLAUDE_BIN}", file=sys.stderr)
        print("   Install Claude Code or set CLAUDE_BIN env var.", file=sys.stderr)
        return 1

    model = CLAUDE_MODEL_OPUS if args.opus else args.model
    library = Path(args.library)
    if not library.exists():
        print(f"❌ library not found: {library}", file=sys.stderr)
        return 1

    # Make library path visible to the tag-step's vocabulary loader.
    global _LIBRARY_PATH
    _LIBRARY_PATH = library

    print(f"📚 Library: {library.name}")
    print(f"🤖 Model:   {model} (via claude CLI / subscription)")
    # Show vocab status so we know it's being used
    try:
        _dict = load_tag_dictionary(library)
        _age = (time.time() - _dict.get("built_at", 0)) / 60
        print(f"📖 Vocab:   en={len(_dict['en'])}, ko={len(_dict['ko'])} "
              f"(age {_age:.0f}m, cache TTL {TAG_DICT_TTL_SECONDS // 60}m)")
    except Exception as _e:  # noqa: BLE001
        print(f"⚠️  Vocab unavailable ({_e}) — using base prompt without dictionary")
    if PROMPT_FOLDERS:
        cat_counts: dict[str, int] = {}
        for _c in PROMPT_FOLDER_CATEGORIES.values():
            cat_counts[_c] = cat_counts.get(_c, 0) + 1
        cat_summary = ", ".join(f"{c}={n}" for c, n in sorted(cat_counts.items()))
        print(f"📝 Prompt folders: {len(PROMPT_FOLDERS)} ({cat_summary})")
    if args.mode != "auto":
        print(f"🔀 Mode filter: {args.mode}")

    # Eagle reachability
    try:
        info = eagle_get("/api/application/info")
        print(f"✅ Eagle:   v{info['data']['version']}")
    except (urllib.error.URLError, KeyError) as e:
        print(f"❌ Eagle API unreachable: {e}", file=sys.stderr)
        return 1

    if args.item_id:
        it = find_item_by_id(library, args.item_id)
        if not it:
            print(f"❌ item not found or no original file: {args.item_id}", file=sys.stderr)
            return 1
        nt, np_ = needs_work(it, force_annotation=args.force_annotation)
        if args.mode == "tags":
            np_ = False
        elif args.mode == "prompt":
            nt = False
        if not (nt or np_):
            print(f"⏩ nothing to do for {args.item_id} "
                  f"(has_tags={it.has_tags}, has_annotation={it.has_annotation})")
            return 0
        items = [it]
        single_mode = item_mode(it)
        steps = []
        if nt: steps.append("tags")
        if np_: steps.append("prompt")
        print(f"🎯 Single item: {it.id} [{single_mode}] {','.join(steps)} — {it.name[:60]!r}")
    else:
        items = list_pending(library, mode_filter=args.mode, force_annotation=args.force_annotation)
        # Count by what each item needs (an item can need both)
        n_need_tags = 0
        n_need_prompt = 0
        prompt_cats: dict[str, int] = {}
        for it in items:
            nt, np_ = needs_work(it, force_annotation=args.force_annotation)
            if args.mode == "tags":
                np_ = False
            elif args.mode == "prompt":
                nt = False
            if nt:
                n_need_tags += 1
            if np_:
                cat = item_prompt_category(it)
                n_need_prompt += 1
                prompt_cats[cat] = prompt_cats.get(cat, 0) + 1
        if prompt_cats:
            cats = ", ".join(f"{c}={n}" for c, n in sorted(prompt_cats.items()))
            print(f"🔍 Pending items: {len(items)} (tags-work={n_need_tags}, prompt-work={n_need_prompt} [{cats}])")
        else:
            print(f"🔍 Pending items: {len(items)} (tags-work={n_need_tags}, prompt-work={n_need_prompt})")

    if args.retry_failed:
        failed_ids: set[str] = set()
        for log_path in (FAILED_LOG, PROMPT_FAILED_LOG):
            if log_path.exists():
                failed_ids.update(
                    json.loads(l)["id"] for l in log_path.read_text().splitlines() if l.strip()
                )
        items = [it for it in items if it.id in failed_ids]
        print(f"🔁 Retrying failed: {len(items)}")
        FAILED_LOG.unlink(missing_ok=True)
        PROMPT_FAILED_LOG.unlink(missing_ok=True)
    elif args.resume:
        done = load_progress()
        before = len(items)
        items = [it for it in items if it.id not in done]
        print(f"⏩ Resuming: skipped {before - len(items)} already-done")

    if args.dry_run:
        items = items[: args.dry_run]
        print(f"🧪 DRY RUN: {len(items)} samples — no DB writes\n")
    elif args.limit:
        items = items[: args.limit]
        print(f"🔢 Limited to first {len(items)} items")

    if not items:
        print("nothing to do")
        return 0

    ok = 0
    err = 0
    t_start = time.time()

    def worker(it: Item) -> ItemResult:
        nt, np_ = needs_work(it, force_annotation=args.force_annotation)
        if args.mode == "tags":
            np_ = False
        elif args.mode == "prompt":
            nt = False
        return process_one(it, model, dry_run=bool(args.dry_run), do_tags=nt, do_prompt=np_)

    def _fmt_err(e: BaseException) -> str:
        return traceback.format_exception_only(type(e), e)[-1].strip()

    with cf.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        future_to_item = {ex.submit(worker, it): it for it in items}
        for i, fut in enumerate(cf.as_completed(future_to_item), 1):
            it = future_to_item[fut]
            try:
                result: ItemResult = fut.result()
            except Exception as e:  # noqa: BLE001 — unexpected worker crash
                err += 1
                msg = f"{type(e).__name__}: {e}"
                print(f"  [{i}/{len(items)}] 💥 {it.id} {it.name[:40]!r} — worker crash: {msg}")
                if not args.dry_run:
                    append_log(FAILED_LOG, {
                        "id": it.id, "name": it.name, "mode": "worker",
                        "error": msg, "trace": _fmt_err(e),
                    })
                continue

            # Build per-step summary line
            steps_done: list[str] = []
            steps_failed: list[str] = []
            if result.tags is not None:
                steps_done.append("tags")
            if result.annotation is not None:
                cat_suffix = f":{result.category}" if result.category else ""
                steps_done.append(f"prompt{cat_suffix}")
            if result.tags_error is not None:
                steps_failed.append("tags")
            if result.prompt_error is not None:
                steps_failed.append("prompt")

            status_icon = "✅" if result.any_success and not result.any_error else (
                "⚠️ " if result.any_success and result.any_error else "❌"
            )
            done_str = "+".join(steps_done) or "—"
            fail_str = f" ✗{','.join(steps_failed)}" if steps_failed else ""
            print(f"  [{i}/{len(items)}] {status_icon} {it.id} [{done_str}{fail_str}] "
                  f"({result.elapsed:.1f}s) {it.name[:40]!r}")

            # Detail print (dry-run shows content; otherwise preview)
            if result.tags is not None:
                if args.dry_run:
                    en, ko = result.tags[:20], result.tags[20:]
                    print(f"       EN: {', '.join(en)}")
                    print(f"       KO: {', '.join(ko)}")
                else:
                    print(f"       tags → {', '.join(result.tags[:5])}, … (+{len(result.tags) - 5})")
            if result.annotation is not None:
                lines = result.annotation.splitlines()
                if args.dry_run:
                    print("       ── annotation ──")
                    for line in lines[:30]:
                        print(f"       {line}")
                    if len(lines) > 30:
                        print(f"       … (+{len(lines) - 30} more lines)")
                else:
                    print(f"       annotation → {len(result.annotation)} chars, {len(lines)} lines")
            if result.tags_error is not None:
                print(f"       ✗ tags: {_fmt_err(result.tags_error)}")
            if result.prompt_error is not None:
                print(f"       ✗ prompt: {_fmt_err(result.prompt_error)}")

            # Bookkeeping
            if result.any_success:
                ok += 1
            if result.any_error:
                err += 1

            if not args.dry_run:
                ts = time.time()
                if result.tags is not None:
                    append_log(PROGRESS_LOG, {
                        "id": it.id, "name": it.name, "mode": "tags",
                        "tags": result.tags, "model": model,
                        "elapsed": result.elapsed, "ts": ts,
                    })
                if result.annotation is not None:
                    append_log(PROMPT_PROGRESS_LOG, {
                        "id": it.id, "name": it.name, "mode": "prompt",
                        "category": result.category,
                        "annotation_len": len(result.annotation),
                        "model": model, "elapsed": result.elapsed, "ts": ts,
                    })
                if result.tags_error is not None:
                    append_log(FAILED_LOG, {
                        "id": it.id, "name": it.name, "mode": "tags",
                        "error": f"{type(result.tags_error).__name__}: {result.tags_error}",
                        "trace": _fmt_err(result.tags_error),
                    })
                if result.prompt_error is not None:
                    append_log(PROMPT_FAILED_LOG, {
                        "id": it.id, "name": it.name, "mode": "prompt",
                        "category": item_prompt_category(it),
                        "error": f"{type(result.prompt_error).__name__}: {result.prompt_error}",
                        "trace": _fmt_err(result.prompt_error),
                    })

    dt = time.time() - t_start
    print(f"\n━━━ done in {dt:.1f}s ━━━")
    print(f"  success: {ok}")
    print(f"  failed:  {err}")
    if ok:
        print(f"  avg:     {dt / (ok + err):.1f}s/item")
    if err and not args.dry_run:
        print(f"  → retry with: python3 {Path(__file__).name} --retry-failed")
        if PROMPT_FAILED_LOG.exists():
            print(f"     (prompt failures logged to {PROMPT_FAILED_LOG.name})")
    return 0 if err == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
