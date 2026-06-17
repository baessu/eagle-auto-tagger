# Getting Started — Claude Code로 받아서 쓰기

Eagle 라이브러리의 이미지를 Claude Vision으로 자동 태깅하는 도구입니다.
별도 API 키 없이 **Claude Code 구독(OAuth)** 으로 동작합니다.

> 이 도구는 **macOS + Eagle 앱**을 전제로 합니다. (watchdog/launchd 자동화는 macOS 전용)

---

## 0. 사전 준비물

| 항목 | 확인 방법 |
|---|---|
| **Eagle 앱** | 설치 후 실행 중이어야 함 ([eagle.cool](https://eagle.cool)). 환경설정 → 개발자 → "API 사용" 켜기 (포트 41595) |
| **Claude Code CLI** | `claude --version` 출력되면 OK. 없으면 [docs.claude.com/claude-code](https://docs.claude.com/claude-code) |
| **Claude 로그인** | `claude` 한 번 실행해 구독 계정으로 로그인되어 있어야 함 (Pro/Max 등) |
| **Python 3.9+** | `python3 --version` |

> 💡 **이 도구는 `ANTHROPIC_API_KEY`를 쓰지 않습니다.** 오히려 환경에 키가 있으면 의도적으로 제거하고 Claude Code 구독 인증으로 호출합니다. 즉 API 종량 과금이 아니라 구독 한도 안에서 돌아갑니다.

---

## 1. 클론 & 의존성 설치

```bash
git clone https://github.com/baessu/eagle-auto-tagger.git
cd eagle-auto-tagger
pip3 install Pillow watchdog
```

> Claude Code 안에서 이 폴더를 열면 `CLAUDE.md`가 자동으로 로드되어, "dry-run 해줘" / "watcher 설치해줘" 같은 자연어 요청을 바로 처리할 수 있습니다.

---

## 2. 라이브러리 경로 설정

Eagle 라이브러리 폴더(`...something.library`)의 **전체 경로**를 지정합니다.
방법은 두 가지 — 아무거나:

**A) 환경변수 (추천)**
```bash
export EAGLE_LIBRARY="$HOME/Eagle/MyLibrary.library"   # 본인 경로로 교체
```

**B) `.env` 파일** — `.env.example`을 복사해서 채우면 watcher가 자동으로 읽습니다.
```bash
cp .env.example .env
# 편집기로 .env 열어서 EAGLE_LIBRARY 값 채우기
```

> 라이브러리 경로를 모르겠다면: Eagle 메뉴 → 파일 → "라이브러리 위치 보기"(Reveal in Finder)로 확인.

---

## 3. 첫 실행 — dry-run으로 품질 확인 (DB 안 건드림)

```bash
python3 auto_tag.py --dry-run 3
```

샘플 3개를 분석해 태그(영문 20 + 한글 20)만 출력합니다. **라이브러리에 아무것도 쓰지 않습니다.** 결과가 괜찮으면 다음 단계로.

---

## 4. 실제 태깅

```bash
# 10개만 적용해보기
python3 auto_tag.py --limit 10

# 전체 적용 (병렬 4개)
python3 auto_tag.py --concurrency 4

# 중단됐으면 이어서
python3 auto_tag.py --resume --concurrency 4
```

태그 없는 이미지만 처리하고, 이미 태그가 있으면 건너뜁니다. 진행/실패는 `.tag_progress.jsonl` / `.tag_failed.jsonl`에 기록됩니다(둘 다 git에 안 올라감).

---

## 5. (선택) 새 이미지 자동 태깅 — 상시 watcher

Eagle에 새 이미지가 들어올 때마다 자동 태깅하려면 launchd LaunchAgent로 watcher를 띄웁니다. 자세한 설치/관리 명령은 **[README.md](README.md)의 "자동 태깅 Hook" 섹션** 참고.

요약:
```bash
# .env 또는 환경변수에 EAGLE_LIBRARY 설정 후
python3 watcher.py        # 포그라운드 테스트 실행 (Ctrl+C로 종료)
```
상시 구동(부팅 시 자동 시작)은 README의 plist 설치 절차를 따르세요.

---

## 6. (선택) 태그 어휘 통일

자유 생성된 동의어(`minimal` vs `minimalist`)를 하나로 모으는 정규화 파이프라인이 있습니다. README의 **"태그 어휘 통일(canonical vocabulary)"** 섹션 참고.

---

## 자주 막히는 곳

| 증상 | 원인 / 해결 |
|---|---|
| `❌ watch dir not found` | `EAGLE_LIBRARY` 경로가 틀렸거나 `.library` 폴더가 아님 |
| `claude: command not found` | Claude Code CLI 미설치 또는 PATH에 없음. `CLAUDE_BIN=/절대/경로/claude` 로 지정 가능 |
| 호출이 API 과금됨 | `ANTHROPIC_API_KEY`가 떠 있어도 이 도구는 제거하고 구독으로 호출함. 그래도 걱정되면 `unset ANTHROPIC_API_KEY` |
| Eagle 연결 실패 (41595) | Eagle 앱이 꺼져 있거나 API가 비활성. 앱 실행 + 개발자 설정 확인 |
| `OSError: Resource deadlock avoided` | OneDrive 등 클라우드 동기화 파일이 "온라인 전용" 상태. 로컬에 내려받은 뒤 재시도 |

---

## 안전장치 (이 도구가 하지 않는 것)

- 외부로 데이터를 보내지 않습니다 — 이미지는 Claude Vision 분석에만 쓰이고, 태그는 **로컬 Eagle**에만 기록됩니다.
- 태그 마이그레이션/그룹 적용 스크립트는 **수정 전 자동 백업**(`.tag_backup_*` / `.tag_groups_backup_*`)을 만들고, `--dry-run`을 지원합니다.
- 시크릿/API 키를 저장하거나 요구하지 않습니다.
