# CLAUDE.md — project context for Claude Code

Eagle(이미지 자산 관리 앱) 라이브러리의 태그 없는 이미지를 Claude Vision으로 분석해
자동 태깅하는 macOS 도구. 인증은 **Claude Code 구독(OAuth)** — `ANTHROPIC_API_KEY`는
서브프로세스 env에서 의도적으로 제거됨(API 종량과금 대신 구독 사용).

## 핵심 파일
- `auto_tag.py` — 메인. 라이브러리 스캔 → Claude vision 호출 → Eagle HTTP API(`:41595`)로 태그 쓰기. 단일 아이템 모드(`--item-id`)도 지원.
- `watcher.py` — watchdog로 `<lib>/images/` 감시 → 신규 metadata.json 발생 시 debounce 후 `auto_tag.py --item-id` 호출. launchd로 상시 구동.
- `build_canonical_map.py` → `migrate_tags.py` — 동의어 정규화 파이프라인(사전 생성 → 라이브러리 일괄 재기록).
- `classify_tags_to_groups.py` → `apply_tag_groups.py` — 태그를 Eagle 태그 그룹으로 분류·적용.
- `com.eagle-auto-tagger.plist.template` — launchd LaunchAgent 템플릿(placeholder 치환용).

## 설정 (전부 환경변수, 하드코딩 경로 없음)
- `EAGLE_LIBRARY` — Eagle 라이브러리 `.library` 폴더 절대경로. 미설정 시 `~/Eagle/MyLibrary.library` placeholder(없으면 명확히 에러).
- `CLAUDE_BIN` — `claude` 바이너리 경로. 기본은 PATH의 `claude`.
- `EAGLE_PROMPT_FOLDERS_PHOTO` / `EAGLE_PROMPT_FOLDERS_ILLUSTRATION` — 콤마 구분 폴더 ID. 해당 폴더 아이템은 태그 + 생성형 프롬프트를 받음.
- `.env`(repo 루트, git 제외)에 위 변수들을 넣으면 watcher가 읽음. `.env.example` 참고.

## 작업 시 주의
- 절대 개인 경로(`/Users/<name>/...`)나 특정 폴더 ID를 코드/문서에 하드코딩하지 말 것 — 환경변수로.
- `.tag_*`, `.prompt_*`, `*.log`, 로컬 `.env`, 머신별 plist는 `.gitignore` 대상. 커밋 금지.
- 라이브러리를 수정하는 스크립트(migrate/apply)는 항상 백업 생성 + `--dry-run` 우선.
- 시크릿/토큰을 추가하지 말 것. 인증은 Claude Code CLI 구독에 위임.

## 빠른 검증
```bash
python3 auto_tag.py --dry-run 3   # DB 안 건드리고 태그 품질만 확인
```

사용자용 시작 가이드는 `GETTING_STARTED.md`, 상세 동작/옵션은 `README.md`.
