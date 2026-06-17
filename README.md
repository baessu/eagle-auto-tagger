# Eagle Auto-Tagger

Eagle 라이브러리의 태그 없는 이미지를 Claude Vision으로 분석해 **영문 20 + 한글 20 = 40개** 태그를 자동 부여합니다. 라이브러리에서 기존에 사용 중인 태그(canonical vocabulary)를 시스템 프롬프트에 주입해, 새 태그가 자유 생성되지 않고 기존 어휘로 수렴하도록 유도합니다.

특정 폴더(예: 화보·레퍼런스 폴더, 일러스트 폴더)의 이미지는 **태그(40개) + 생성형 AI 재현 프롬프트**를 모두 받습니다. 프롬프트는 폴더의 성격에 따라 두 카테고리로 라우팅:

- **photo** — 사진·화보·에디토리얼·룩북·제품샷. 9-layer 시네마토그래피/포토그래피 프로토콜
- **illustration** — 일러스트·애니메이션·콘셉트 아트·만화·페인팅. 10-layer 일러스트레이션 프로토콜

각 카테고리는 별도 환경변수 (`EAGLE_PROMPT_FOLDERS_PHOTO`, `EAGLE_PROMPT_FOLDERS_ILLUSTRATION`)로 지정합니다. 일반 폴더 아이템은 기존대로 태그만 받습니다.

## 준비

```bash
# 1) Claude Code CLI 설치 (구독 OAuth 인증 사용 — API 키 불필요)
#    https://docs.claude.com/claude-code

# 2) Pillow (이미지 리사이즈용, HEIC/BMP/TIFF 등 변환 필수)
pip3 install Pillow watchdog

# 3) Eagle 앱이 실행 중이어야 함 (포트 41595)
```

> `ANTHROPIC_API_KEY`가 환경에 있으면 의도적으로 제거하고 CLI 구독을 사용합니다(`auto_tag.py:234`).

## 사용법

```bash
# 샘플 3개만 분석 (DB 쓰기 없음) — 태그 품질 확인용
python3 auto_tag.py --dry-run 3

# 10개만 실제 적용 (auto 라우팅 — 폴더에 따라 tags/prompt 자동 결정)
python3 auto_tag.py --limit 10

# 전체 적용 (병렬 4개)
python3 auto_tag.py --concurrency 4

# 프롬프트 모드만 실행 (모든 카테고리 — photo + illustration)
python3 auto_tag.py --mode prompt --concurrency 2

# 프롬프트 모드 dry-run으로 1개 미리보기
python3 auto_tag.py --mode prompt --dry-run 1

# 기존 annotation 덮어쓰기
python3 auto_tag.py --mode prompt --force-annotation

# 태그 모드만 실행 (프롬프트 폴더 아이템은 건너뜀)
python3 auto_tag.py --mode tags --concurrency 4

# 중단 후 재개
python3 auto_tag.py --resume --concurrency 4

# 실패한 것만 재시도 (tags/prompt 둘 다)
python3 auto_tag.py --retry-failed

# 더 깐깐한 분석 필요하면 Opus로
python3 auto_tag.py --opus --dry-run 3
```

## 프롬프트 모드

폴더의 성격에 맞춰 두 카테고리 중 하나의 시스템 프롬프트로 분석이 진행됩니다.

```bash
# 사진/화보 계열 — 9-layer cinematography/photography 분석
# 콤마로 구분된 Eagle 폴더 ID로 교체 (Eagle에서 폴더 우클릭 → "폴더 ID 복사")
export EAGLE_PROMPT_FOLDERS_PHOTO="FOLDER_ID_1,FOLDER_ID_2"

# 일러스트 계열 — 10-layer illustration 분석
export EAGLE_PROMPT_FOLDERS_ILLUSTRATION="FOLDER_ID_3"
```

> 한 폴더 ID는 한 카테고리에만 속해야 합니다. 같은 ID를 양쪽에 넣으면 PHOTO → ILLUSTRATION → legacy 순으로 먼저 매칭된 게 우선합니다.
>
> 한 아이템이 두 카테고리 폴더에 동시에 들어있다면, `folders` 배열의 앞쪽 폴더가 우선됩니다.

### 카테고리별 출력 구조

| 카테고리 | 분석 레이어 | 마지막 섹션 |
|---|---|---|
| **photo** | SUBJECT / COMPOSITION / LIGHTING / MATERIAL & TEXTURE / COLOR SYSTEM / BACKGROUND & ENVIRONMENT / STYLE & RENDERING / MOOD & BRAND IMPRESSION | A. full prompt / B. core prompt / C. negative / D. model-specific notes |
| **illustration** | SUBJECT & CHARACTER DESIGN / SHAPE LANGUAGE / LINEWORK / COLOR & LIGHTING SYSTEM / RENDERING & PAINTING TECHNIQUE / COMPOSITION & CINEMATIC / STYLE DNA & ARTISTIC INFLUENCE / ENVIRONMENT & WORLD DESIGN / MOOD & EMOTIONAL IMPRESSION | A. full prompt / B. core prompt (Niji, NovelAI 등 포함) / C. negative / D. style+rendering tags / E. model-specific notes |

### 분기 규칙

prompt 폴더 아이템은 **태그와 프롬프트 둘 다** 받습니다. 각 단계는 독립적이라 한쪽이 실패해도 다른 쪽은 진행되며, 이미 채워진 항목은 건너뜁니다.

| 케이스 | tags 작업 | prompt 작업 |
|---|---|---|
| 일반 폴더 + 태그 없음 | 실행 | — |
| 일반 폴더 + 태그 있음 | 스킵 | — |
| prompt 폴더 + 둘 다 없음 | 실행 | 실행 |
| prompt 폴더 + 태그만 있음 | 스킵 | 실행 |
| prompt 폴더 + annotation만 있음 | 실행 | 스킵 (단 `--force-annotation` 시 재실행) |
| prompt 폴더 + 둘 다 있음 | 스킵 | 스킵 (단 `--force-annotation` 시 prompt 재실행) |

- `folders` 배열 ∩ photo 화이트리스트 → prompt 단계는 photo 분석 프롬프트
- `folders` 배열 ∩ illustration 화이트리스트 → prompt 단계는 illustration 분석 프롬프트
- `--mode tags` / `--mode prompt`로 한쪽 단계만 실행하도록 강제 가능

### 로그 파일

- 태그 모드: `.tag_progress.jsonl` / `.tag_failed.jsonl`
- 프롬프트 모드: `.prompt_progress.jsonl` / `.prompt_failed.jsonl` (각 record에 `category` 필드 포함)

### 호환성

기존 `EAGLE_PROMPT_FOLDERS` 환경변수는 그대로 인식되며 자동으로 `photo` 카테고리로 매핑됩니다.

## 동작

1. `<라이브러리>.library/images/*/metadata.json` 디스크 직접 스캔 → 태그 없는 이미지 수집 (Eagle API는 limit 461에서 잘림)
2. 각 이미지를 1568px로 리사이즈 → base64 → Claude `messages` API (vision)
3. JSON 응답 파싱 → `POST /api/item/update`로 태그 쓰기
4. 쓰기 직후 응답 태그와 비교해 **인코딩 깨짐 검증** (한글 일부 글자 깨짐 이슈 대응)
5. 성공은 `.tag_progress.jsonl`, 실패는 `.tag_failed.jsonl`에 기록

## 예상 비용/시간 (Sonnet 4.5 기준)

- 이미지당 ~8초, 병렬 4 → 450개 ≈ 15분
- 이미지당 입력 ~500토큰, 출력 ~400토큰
- 450개 ≈ **$1.5~3** (Sonnet), Opus 사용 시 ~5배

## 로그 구조

```jsonl
// .tag_progress.jsonl
{"id":"MO1GRH...","name":"...","tags":["moody",...],"model":"claude-sonnet-4-5","elapsed":7.8,"ts":...}

// .tag_failed.jsonl
{"id":"XYZ","name":"...","error":"tag mismatch after write: missing={'발랄한'}",...}
```

## 태그 규칙

시스템 프롬프트에 명시:
- 영문 20개: 감상 형용사 10 + 카테고리/키워드 10 (lowercase-kebab-case)
- 한글 20개: 감상 형용사 10 + 카테고리/키워드 10 (띄어쓰기 없이)
- 이미지 내 텍스트/워터마크 제외
- 추상적 찬사(`beautiful`, `멋진`) 금지

## 자동 태깅 Hook (launchd 상시 구동)

새 이미지가 Eagle에 들어오면 파일시스템 이벤트를 감지해 자동으로 태깅합니다.

### 구성 요소

| 파일 | 역할 |
|---|---|
| `watcher.py` | FSEvents(watchdog) 기반 라이브러리 감시자. metadata.json 이벤트 → 8초 debounce → `auto_tag.py --item-id` 호출 |
| `com.eagle-auto-tagger.plist.template` | launchd LaunchAgent 템플릿 (경로 placeholder 포함) |
| `auto_tag.py --item-id ID` | 단일 아이템 처리 모드. 이미 태그 있으면 skip |

### 새 머신에서 설치 (사용자명/경로가 다른 경우)

```bash
# 1) clone
git clone https://github.com/baessu/eagle-auto-tagger.git
cd eagle-auto-tagger

# 2) 의존성
pip3 install Pillow watchdog

# 3) plist 생성 — 경로/환경변수 치환
PYTHON_BIN="$(which python3)"
PROJECT_DIR="$(pwd)"
EAGLE_LIBRARY_PATH="$HOME/Eagle/MyLibrary.library"   # 본인 Eagle 라이브러리 경로로 교체
# 콤마로 구분된 Eagle 폴더 ID (없으면 빈 문자열로)
EAGLE_PROMPT_FOLDERS_PHOTO="FOLDER_ID_1,FOLDER_ID_2"
EAGLE_PROMPT_FOLDERS_ILLUSTRATION="FOLDER_ID_3"
PLIST="$HOME/Library/LaunchAgents/com.eagle-auto-tagger.plist"

sed -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__EAGLE_LIBRARY_PATH__|$EAGLE_LIBRARY_PATH|g" \
    -e "s|__EAGLE_PROMPT_FOLDERS_PHOTO__|$EAGLE_PROMPT_FOLDERS_PHOTO|g" \
    -e "s|__EAGLE_PROMPT_FOLDERS_ILLUSTRATION__|$EAGLE_PROMPT_FOLDERS_ILLUSTRATION|g" \
    com.eagle-auto-tagger.plist.template > "$PLIST"

# 4) 시작
launchctl load -w "$PLIST"

# 5) 확인
launchctl list | grep eagle-auto-tagger
tail -f watcher.log
```

### 관리 명령

```bash
PLIST="$HOME/Library/LaunchAgents/com.eagle-auto-tagger.plist"

# 중지
launchctl unload "$PLIST"

# 재시작 (코드 수정 후)
launchctl unload "$PLIST" && launchctl load -w "$PLIST"
```

### 동작 원리

1. `<라이브러리>.library/images/` 디렉토리를 재귀 감시
2. `{ID}.info/metadata.json` 생성/수정 이벤트 포착
3. **Debounce 8초** — Eagle이 import 중 metadata.json을 여러 번 업데이트하므로 마지막 쓰기 이후 8초 조용해지면 트리거
4. `auto_tag.py --item-id <ID>`를 subprocess로 호출
5. 해당 아이템이 이미 태그 있으면 즉시 skip (중복 방지)

### 주의

- 인증은 Claude Code CLI의 구독 OAuth를 사용합니다. `claude` 명령이 이미 로그인된 상태여야 함
- OneDrive 동기화로 다른 기기에서 추가된 이미지도 자동 태깅됩니다 — **단** OneDrive 파일이 "클라우드 전용(Files On-Demand)"이면 PIL이 잠금에 실패해 `OSError: [Errno 11] Resource deadlock avoided`가 납니다. 여러 기기에 watcher를 띄워두면 둘 중 누군가는 로컬 사본을 갖고 있어 성공률이 올라갑니다
- Eagle을 끈 상태에서 이미지가 라이브러리에 추가되면 태깅 실패 → Eagle 재실행 후 `auto_tag.py --resume`으로 일괄 처리 가능

## 태그 어휘 통일 (canonical vocabulary)

매번 자유 생성하면 동의어가 분산됩니다 (`minimal` vs `minimalist`, `modern` vs `contemporary`, `심플한` vs `미니멀한`). 두 단계로 통일:

### 1) 사전 + 마이그레이션 (한 번)

```bash
# 1. canonical map 생성 (LLM이 동의어 묶어줌, 영/한 top 400씩)
python3 build_canonical_map.py --top 400 --chunk-size 80 --timeout 180

# 결과 검토 (markdown 표 형식)
open .tag_canonical_map_review.md
# 잘못된 매핑은 .tag_canonical_map.json 직접 편집

# 2. 라이브러리 전체 태그를 canonical로 재기록 (백업 자동 생성)
python3 migrate_tags.py --dry-run --limit 10   # 미리보기
python3 migrate_tags.py --limit 10             # 소규모 테스트
python3 migrate_tags.py --concurrency 4        # 전체 적용
```

전체 마이그레이션은 882 아이템 기준 2-3초. 실패 시 `.tag_backup_<timestamp>.jsonl`에서 복원 가능.

### 2) 사전 자동 통합 (자동)

`auto_tag.py`는 호출 때마다 `.tag_dictionary_cache.json`을 읽어 시스템 프롬프트에 `PREFERRED VOCABULARY` 섹션을 주입합니다.

- 영/한 각 빈도 ≥ 2, 상위 400개씩
- 캐시 TTL 6시간 — 그 안에는 동일 사전 사용 (Claude prompt caching 활용)
- Eagle 라이브러리가 변하면 6시간 후 자동 재빌드, 또는 캐시 파일 삭제로 강제 재빌드

새로 들어오는 이미지의 태그는 사전 어휘로 우선 수렴하고, 어울리는 게 없을 때만 신규 어휘를 만듭니다.

## 알려진 이슈

- **한글 인코딩**: Eagle HTTP API는 UTF-8 JSON 정상 처리, MCP 레이어만 문제 있음. 본 스크립트는 HTTP 직접 호출이라 안전하며, write 후 round-trip 검증까지 수행.
- **Eagle `/api/item/list` limit cap**: ~461개에서 잘림. 때문에 디스크 스캔 방식 사용.
- **Eagle 검색은 정확 매칭**: 부분 매칭 안 됨. 그래서 동의어 통일이 검색 정확도에 직접 영향.
