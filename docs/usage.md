# Usage

## 기본 운영 흐름

AI review는 manual by default입니다. PR open, synchronize, reopen만으로는 reviewer가 실행되지 않습니다.

```text
PR 열기
-> 필요한 시점에 PR 댓글 작성
-> @glm 리뷰해줘, @미니맥스 리뷰해줘, @딥시크 리뷰해줘, @지피티높음 리뷰해줘, @지피티매우높음 리뷰해줘, @지피티확장 리뷰해줘, @클로드 리뷰해줘, @클로드-p 리뷰해줘, @코덱스 리뷰해줘, 또는 @최종리뷰
-> 중앙 webhook daemon이 issue_comment.created event 처리
-> PR comment로 리뷰 결과 게시
```

예시:

```text
@glm 리뷰해줘
@미니맥스 리뷰해줘
@딥시크 동시성 문제랑 데이터 유실 가능성 중심으로 봐
@지피티높음 리뷰해줘
@지피티매우높음 리뷰해줘
@지피티확장 리뷰해줘
@클로드 리뷰해줘
@클로드-p 리뷰해줘
@코덱스 리뷰해줘
@최종리뷰
```

같은 PR에 같은 reviewer 멘션 댓글을 여러 개 남기면 comment id별로 각각 별도
review job이 큐잉됩니다. 여러 번 호출하려면 `@지피티매우높음 코드리뷰`처럼
멘션이 포함된 댓글을 여러 개 만들면 됩니다. 단독 `코드리뷰` 또는 `코드 리뷰`
댓글은 트리거하지 않습니다.

`@최종리뷰`는 Codex를 더 높은 reasoning effort로 실행해 merge readiness를
판단합니다. 연결 이슈, PR 본문/코멘트/리뷰, 관련 repo와 운영 맥락을 읽기
전용으로 확인하고 blocker와 non-blocking note를 구분합니다.

`@지피티높음`, `@지피티매우높음`, `@지피티확장`은 운영 서버의 CloakBrowser 기반 ChatGPT 웹 UI와 GitHub 앱 연동으로 직접 PR 리뷰를 요청하는 경로입니다. 현재 기본 매핑은 다음과 같습니다.

- `@지피티높음` → ChatGPT 추론 수준 `높음`
- `@지피티매우높음` → 단일 CDP 세션, ChatGPT 추론 수준 `매우 높음`
- `@지피티확장` → 단일 CDP 세션, ChatGPT 추론 수준 `Pro`

모든 reviewer 실행 프롬프트는 같은 코드 리뷰 지시문을 기준으로 쓰고, 자동 실행에는
GitHub 게시 위치, 완료 marker, 실패 fallback 같은 최소 운영 지시만 덧붙입니다.
ChatGPT 경로는 전송 전 composer에서 GitHub 앱을 attach하고, 30초 후 응답 handoff가 비정상이면 같은 채팅에 GitHub 앱을 다시 attach한 fallback을 보냅니다.

## Webhook daemon review

현재 권장 경로는 대상 repo에 GitHub webhook을 등록하고, 운영 서버의 daemon이 로컬
`gh`, `opencode`, ChatGPT 웹 UI(CloakBrowser), `claude`, `claude-p`, `codex` 인증으로 리뷰를 수행하는 방식입니다. 자세한
설치는 `docs/webhook-setup.md`를 따릅니다.

기본 조건:

- GitHub webhook HMAC SHA-256 signature가 유효해야 합니다.
- `issue_comment.created` event여야 합니다.
- 댓글이 PR에 달려 있어야 합니다.
- base repo가 `allowed_repositories`에 있어야 합니다.
- comment 작성자의 `author_association`은 기본적으로 `OWNER`여야 합니다.
- org repo는 repo별 설정으로 `MEMBER`/`COLLABORATOR`를 추가 허용할 수 있습니다.
- 같은 comment id는 한 번만 처리합니다.
- 요청은 이전 reviewer 완료를 기다리지 않고 독립적으로 실행합니다. 외부 reviewer
  프로세스 시작만 기본 15초 간격으로 분산하며, 실행 중인 reviewer끼리는 병렬입니다.
  ChatGPT job은 작업별 독립 페이지와 ChatGPT `/` 경로의 새 채팅을 사용하되, 같은 계정의
  prompt 전송은 CDP 슬롯 전체에서 하나씩만 진행합니다. 세션 생성/전송 확인 후 즉시
  rocket을 남기고, 다음 전송은 기본 45~75초의 랜덤 간격 뒤에 시작합니다. 이전 generation
  답변은 기다리지 않습니다. prompt 전송 전 403·네트워크 실패의 재시도 cooldown 기본값은
  90초, 150초, 300초입니다.
  daemon 재시작 시 일반 reviewer의 `running` job은 다시 실행 대상으로 복구하지만,
  ChatGPT `running` job은 전송 여부가 불명확할 수 있어 `failed`로 남기고 재개하지 않습니다.
  ChatGPT marker가 이미 PR에 있으면 중복 리뷰 없이 완료 처리합니다.
  `@glm`, `@미니맥스`, `@딥시크`는 고정 session title과 persistent workspace를 사용하므로, daemon 재시작 뒤에도 같은
  OpenCode 세션을 이어붙일 수 있으면 같은 session으로 계속 진행합니다. review session title은
  `opencode session list` 최근 목록에도 그대로 보입니다.
- 일반 reviewer 실행 뒤 marker가 게시되지 않으면 최대 4회까지 지수 backoff로 자동 재시도합니다.
  ChatGPT는 CDP 연결 실패처럼 prompt 전송 전임을 확실히 알 수 있는 경우에만 재시도하며,
  브라우저 프로세스가 시작된 뒤 marker 확인이 실패하면 성공했을 가능성을 보존하기 위해
  `failed`로 기록하고 새 ChatGPT 채팅을 자동으로 다시 열지 않습니다.
- marker 포함 inline review comment 또는 PR review body가 기본 성공 경로입니다.
  reviewer가 marker 포함 일반 PR 대화 댓글을 직접 남긴 경우도 성공으로 처리합니다.
- daemon은 marker 미확인 실패에 대해 별도 fallback/오류 PR comment를 자동 작성하지 않습니다.
  세부 원인은 운영 서버의 review log와 reviewer 세션에서 확인합니다.
- ChatGPT reviewer를 쓰는 경우 운영 서버의 ChatGPT 브라우저 프로필이 이미 로그인되어 있어야 합니다.
- 콜드 스타트 직후 Cloudflare 사람 확인(403 / 「잠시만 기다리십시오…」)이 뜰 수 있다. `chatgpt-github-review`는
  403을 즉시 실패로 보지 않고 composer(`#prompt-textarea`)가 준비될 때까지 대기·챌린지 상호작용·제한
  리로드를 수행한다(기본 최대 약 120초). 전체 ChatGPT job 타임아웃은 300초다.
- ChatGPT pre-send 실패 시 상세 로그/스크린샷은 임시 workdir가 아니라
  `~/.cache/personal-review-machines-chatgpt/failures/`에 보존된다.

## Legacy private repo mention review

아래 reusable workflow 방식은 legacy reference입니다. 새 repo 연동에는 webhook
daemon 방식을 우선 사용하세요.

대상 private repo에 `templates/private-repo-mention-review.yml`을 `.github/workflows/ai-review.yml`로 복사합니다.

필요한 secret:

- `@claude`/`@클로드`: `ANTHROPIC_API_KEY` 또는 `CLAUDE_CODE_OAUTH_TOKEN`
- `@클로드-p`: `CLAUDE_CODE_OAUTH_TOKEN`
- `@opencode`: `ZAI_API_KEY`

여러 reviewer를 쓸 repo에는 필요한 reviewer secret을 모두 넣고, 하나만 쓸 repo에는 필요한 secret만 넣으면 됩니다. `@claude`/`@클로드`는 일반 Claude Code headless 호출이고, API key 방식이면 `ANTHROPIC_API_KEY`, subscription/OAuth 방식이면 `CLAUDE_CODE_OAUTH_TOKEN`을 사용합니다. `@클로드-p`는 Claude Code subscription/OAuth 세션용 wrapper라 `CLAUDE_CODE_OAUTH_TOKEN`을 사용합니다.

## PR head checkout

Webhook daemon review는 요청이 매칭되면 base repo의 `refs/pull/<number>/head`를
임시 checkout합니다. reviewer는 diff/context만 보지 않고 직접 관련된 코드, 테스트,
schema, migration, config, API contract를 읽기 전용으로 확인할 수 있습니다.

금지되는 작업:

- 테스트, 빌드, package manager, formatter, generator 실행
- network command 실행
- write/edit/delete, commit, push, branch 생성
- PR code 실행

## Legacy Claude 인증 방식

`@claude`/`@클로드`에서 `CLAUDE_CODE_OAUTH_TOKEN`을 쓰는 경우 reusable workflow는 Claude Code를 non-`--bare` 모드로 실행합니다. Claude Code의 `--bare` 모드는 OAuth와 keychain auth를 읽지 않고 `ANTHROPIC_API_KEY`만 사용하므로 OAuth token 경로에서는 맞지 않습니다.

`ANTHROPIC_API_KEY`와 `CLAUDE_CODE_OAUTH_TOKEN`이 둘 다 전달되면 OAuth token을 우선하고, Claude 실행 시 `ANTHROPIC_API_KEY`는 비워서 API key 경로로 잘못 빠지지 않게 합니다.

## Legacy OpenCode 첫 실행

GitHub-hosted runner에서 fresh OpenCode가 one-time sqlite migration만 수행하고 review 없이 종료하는 경우가 있습니다. mention workflow는 출력에 `sqlite-migration:done`이 있으면 같은 review command를 한 번 재시도합니다.

## Review artifacts

Webhook daemon은 `work_dir` 아래 임시 디렉터리에 checkout, prompt, log, failure
comment 파일을 둡니다. legacy workflow의 `context.md`와 `pr.diff`는
`GITHUB_WORKSPACE/.tmp/mention-pr-review/<run>-<comment>/` 아래에 저장됩니다.

OpenCode reviewer는 같은 PR/comment의 세션 재개를 위해 workspace를 유지한다. 누적을
막기 위해 `ops/systemd/personal-review-workspace-prune.timer`를 설치하면, 실행 중인
workspace는 건드리지 않고 7일보다 오래된 비활성 workspace를 매일 정리한다. 즉시
점검은 아래처럼 dry-run으로 먼저 한다.

```bash
python3 scripts/prune-review-workspaces.py \
  --work-dir ~/.local/state/personal-review-machines/work
```

`--all-inactive --apply`는 보존 기간을 무시하므로, 현재 실행 중인 프로세스가 없는지
확인한 일회성 정리에서만 사용한다.

## public repo 사용

public repo에서도 기본은 mention-triggered review입니다. 요청이 매칭되면 base repo의 `refs/pull/<number>/head`를 임시 checkout하지만, PR code 실행과 쓰기 작업은 금지하고 읽기 전용 탐색만 허용합니다.

comment 작성자는 기본 정책상 `OWNER`여야 합니다. repo별 override가 있는 org repo는
설정된 `MEMBER`/`COLLABORATOR`도 호출할 수 있습니다.

## Legacy 옵션 조정

리뷰 언어:

```yaml
with:
  language: Korean
```

리뷰 강도:

```yaml
with:
  review_level: security
```

## Legacy 자동 리뷰 템플릿

자동 `pull_request` 리뷰 템플릿은 `templates/experimental-auto/` 아래에 보관합니다. 필요한 경우에만 선택적으로 사용하세요.

기본 운영에서는 자동 리뷰를 권장하지 않습니다. PR을 열거나 push할 때마다 Actions minutes와 model credit이 소모되기 때문입니다.

## 비활성화

Webhook daemon 경로에서는 target repo webhook을 비활성화하고 allowlist에서 repo를
제거하면 AI review가 꺼집니다. legacy reusable workflow 경로에서는 대상 repo의
caller workflow 파일을 삭제하거나 trigger를 차단합니다.
