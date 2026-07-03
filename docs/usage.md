# Usage

## 기본 운영 흐름

AI review는 manual by default입니다. PR open, synchronize, reopen만으로는 reviewer가 실행되지 않습니다.

```text
PR 열기
-> 필요한 시점에 PR 댓글 작성
-> @오픈코드 리뷰해줘, @지피티높음 리뷰해줘, @지피티매우높음 리뷰해줘, @지피티확장 리뷰해줘, @클로드 리뷰해줘, @클로드-p 리뷰해줘, @코덱스 리뷰해줘, 또는 @최종리뷰
-> 중앙 webhook daemon이 issue_comment.created event 처리
-> PR comment로 리뷰 결과 게시
```

예시:

```text
@오픈코드 리뷰해줘
@오픈코드 동시성 문제랑 데이터 유실 가능성 중심으로 봐
@지피티높음 리뷰해줘
@지피티매우높음 리뷰해줘
@지피티확장 리뷰해줘
@클로드 리뷰해줘
@클로드-p 리뷰해줘
@코덱스 리뷰해줘
@최종리뷰
```

`@최종리뷰`는 Codex를 더 높은 reasoning effort로 실행해 merge readiness를
판단합니다. 연결 이슈, PR 본문/코멘트/리뷰, 관련 repo와 운영 맥락을 읽기
전용으로 확인하고 blocker와 non-blocking note를 구분합니다.

`@지피티높음`, `@지피티매우높음`, `@지피티확장`은 운영 서버의 headed Chromium +
`agbrowse`로 ChatGPT 웹 UI를 열고 GitHub 연동으로 직접 PR 댓글을 남기는
경로입니다. 현재 기본 매핑은 다음과 같습니다.

- `@지피티높음` → ChatGPT `thinking` + `extended`
- `@지피티매우높음` → ChatGPT `thinking` + `heavy`
- `@지피티확장` → ChatGPT `pro` + `extended`

## Webhook daemon review

현재 권장 경로는 대상 repo에 GitHub webhook을 등록하고, 운영 서버의 daemon이 로컬
`gh`, `opencode`, `agbrowse`, `claude`, `claude-p`, `codex` 인증으로 리뷰를 수행하는 방식입니다. 자세한
설치는 `docs/webhook-setup.md`를 따릅니다.

기본 조건:

- GitHub webhook HMAC SHA-256 signature가 유효해야 합니다.
- `issue_comment.created` event여야 합니다.
- 댓글이 PR에 달려 있어야 합니다.
- base repo가 `allowed_repositories`에 있어야 합니다.
- comment 작성자의 `author_association`은 기본적으로 `OWNER`여야 합니다.
- org repo는 repo별 설정으로 `MEMBER`/`COLLABORATOR`를 추가 허용할 수 있습니다.
- 같은 comment id는 한 번만 처리합니다.
- 요청은 SQLite job queue에 저장한 뒤 `job_worker_count` 개 worker가 병렬 처리합니다.
  daemon 재시작 시 `running` job은
  다시 `queued`로 돌려 재개하고, marker가 이미 PR에 있으면 중복 리뷰 없이 완료 처리합니다.
  `@오픈코드`는 고정 session title과 persistent workspace를 사용하므로, daemon 재시작 뒤에도 같은
  OpenCode 세션을 이어붙일 수 있으면 같은 session으로 계속 진행합니다. review session title은
  `opencode session list` 최근 목록에도 그대로 보입니다.
- reviewer 실행 뒤 marker가 게시되지 않으면 자동 재시도 없이 fallback 보존 경로를 시도합니다.
- marker 포함 inline review comment 또는 PR review body가 기본 성공 경로입니다.
  둘 다 막히면 marker 포함 일반 PR 대화 댓글로 리뷰 산출물과 로그 tail을 보존하고
  성공으로 처리합니다.
- 일반 PR 대화 댓글도 실패하면 reviewer가 marker 없이 채팅 응답에 리뷰 내용을 남기도록
  지시하고, daemon은 추출 가능한 내용을 log/failure artifact에 보존합니다.
- ChatGPT reviewer를 쓰는 경우 운영 서버의 ChatGPT 브라우저 프로필이 이미 로그인되어 있어야 합니다.

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
