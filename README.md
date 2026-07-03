# Personal Review Machines

GitHub repo들의 AI PR 리뷰 요청을 운영 서버에서 처리하는 webhook 서비스입니다.

기본 구조는 GitHub Actions self-hosted runner를 늘리지 않습니다. 각 대상 repo에
GitHub webhook만 설치하고, 이 서버의 daemon이 `issue_comment` 이벤트를 받아 로컬
`gh`, `opencode`, `agbrowse(ChatGPT)`, `claude`, `claude-p`, `codex`로 리뷰를 실행한 뒤 원 PR에 댓글을 답니다.
다른 repo들은 이 repo의 workflow를 호출하는 것이 아니라, 같은 webhook
endpoint를 등록해서 사용합니다.

## Flow

```text
PR 댓글 생성
-> GitHub webhook이 운영 서버 endpoint 호출
-> HMAC signature 검증
-> repo allowlist + author_association 정책 확인
-> @오픈코드 / @지피티높음 / @지피티매우높음 / @지피티확장 / @클로드 / @클로드-p / @코덱스 멘션 파싱
-> gh로 PR context/diff/최근 댓글/리뷰 수집
-> 로컬 opencode/agbrowse(ChatGPT)/claude/claude-p/codex 실행
-> marker 포함 inline review comment, PR review body, 또는 fallback PR comment가 실제 게시됐는지 확인
-> 게시 확인 실패 시 추출 가능한 리뷰 산출물과 로그 tail을 PR comment로 보존
```

리뷰는 자동 실행되지 않습니다. PR을 열거나 push해도 아무 일도 하지 않고, repo
owner가 PR 댓글 첫머리에 아래 멘션을 남길 때만 실행합니다.

```text
@오픈코드 리뷰해줘
@오픈코드 동시성 문제 중심으로 봐
@지피티높음 리뷰해줘
@지피티매우높음 리뷰해줘
@지피티확장 리뷰해줘
@클로드 리뷰해줘
@클로드-p 리뷰해줘
@코덱스 리뷰해줘
```

public/private repo 모두 기본 호출자 정책은 `OWNER`입니다. org repo처럼 운영상
필요한 경우 repo별로 `MEMBER`/`COLLABORATOR`를 추가 허용할 수 있습니다.

## Repository Shape

```text
server/
  app.py              # stdlib HTTP webhook receiver
  config.py           # JSON config loader
  review_runner.py    # gh + local AI tool execution
  store.py            # SQLite processed-comment store
config.example.json
ops/systemd/
  personal-review-machines.service
  personal-review-machines-chatgpt-browser.service
docs/
  security.md
  setup.md
  webhook-setup.md
  webhook-canary-example.md
```

기존 reusable workflow와 template들은 legacy reference로 남겨 둡니다. 지금 권장
방식은 Actions runner가 아니라 webhook daemon입니다.

## Setup

1. 설정 파일을 만듭니다.

```bash
cp config.example.json config.json
```

`config.json`은 로컬 운영 파일입니다. 실제 webhook secret, 실제 webhook URL,
운영 repo allowlist는 여기에만 두고 commit하지 않습니다.

2. `config.json`에서 webhook URL, webhook secret, 허용 repo를 설정합니다.

```json
{
  "webhook_secret": "replace-with-long-random-secret",
  "webhook_url": "https://review.example.com/github-webhook",
  "allowed_repositories": ["YOUR_GITHUB_ID/YOUR_REPO"],
  "allowed_author_associations": ["OWNER"],
  "repository_author_associations": {
    "YOUR_ORG/YOUR_REPO": ["OWNER", "MEMBER", "COLLABORATOR"]
  },
  "work_dir": "/var/lib/personal-review-machines/work",
  "db_path": "/var/lib/personal-review-machines/reviews.sqlite3",
  "bind_host": "127.0.0.1",
  "bind_port": 18080,
  "job_max_attempts": 1,
  "job_retry_delay_seconds": 0,
  "job_poll_seconds": 5,
  "job_worker_count": 3
}
```

3. 대상 repo의 webhook을 추가합니다.

CLI로 설치:

```bash
scripts/install-webhook.py YOUR_GITHUB_ID/YOUR_REPO --write-config
sudo systemctl restart personal-review-machines.service
```

수동 설치:

- Payload URL: `config.json`의 `webhook_url`
- Content type: `application/json`
- Secret: `config.json`의 `webhook_secret`
- Events: `Issue comments`

4. 운영 서버에서 로컬 도구 인증을 확인합니다.

```bash
gh auth status
opencode --version
agbrowse --help
claude --version
claude-p --version
codex --version
```

5. systemd service를 설치합니다.

```bash
sudo install -m 644 ops/systemd/personal-review-machines.service /etc/systemd/system/personal-review-machines.service
sudo install -m 644 ops/systemd/personal-review-machines-chatgpt-browser.service /etc/systemd/system/personal-review-machines-chatgpt-browser.service
sudo systemctl daemon-reload
sudo systemctl enable --now personal-review-machines-chatgpt-browser.service
sudo systemctl enable --now personal-review-machines.service
```

nginx나 터널은 `/github-webhook` 요청을 `http://127.0.0.1:18080/github-webhook`
으로 넘기면 됩니다.

공개 repo로 운영할 때도 실제 도메인, webhook secret, 운영 allowlist는
`config.json`에만 보관합니다. tracked 문서와 예시는 `review.example.com`,
`YOUR_GITHUB_ID/YOUR_REPO`, `YOUR_ORG/YOUR_REPO` 같은 placeholder만 사용합니다.

## Safety Model

- GitHub webhook HMAC SHA-256 signature를 검증합니다.
- `issue_comment.created`만 처리합니다.
- PR 댓글만 처리합니다.
- 기본 호출자 정책은 `comment.author_association == OWNER`입니다. repo별
  `repository_author_associations` override로 org repo에 `MEMBER`/`COLLABORATOR`를
  추가 허용할 수 있습니다.
- repo allowlist에 없는 repo는 거부합니다.
- fork된 repo에서 온 webhook event는 거부합니다. 공개 repo를 누군가 fork해가도
  그 fork repo는 운영 서버의 review service를 사용할 수 없습니다.
- public fork PR도 같은 흐름으로 처리하되, repo owner가 직접 멘션한 경우에만
  실행합니다.
- PR head는 GitHub의 `refs/pull/<number>/head`로 checkout합니다. fork repo의
  secret이나 Actions 권한은 사용하지 않습니다.
- 처리한 comment id는 SQLite에 저장해 중복 실행을 막습니다.
- 모델이 GitHub 게시 없이 채팅 응답에만 리뷰를 남기면 실패입니다. marker 포함
  inline review comment, PR review body, 또는 fallback PR comment가 실제 PR에
  게시되면 성공으로 봅니다.
- PR code 실행, build, test, install은 리뷰 지시에서 금지합니다.
- `codex`는 파일 수정 없이 읽기 전용 리뷰로만 사용합니다. sandbox와 host
  integration 설정을 바꿀 때도 PR code 실행/build/test/install 금지와 marker
  게시 확인은 유지해야 합니다.
- `@클로드`는 일반 Claude Code headless 호출인 `claude -p`로 실행합니다.
  허용된 read/write/gh shell 도구를 비대화로 통과시키는 권한 모드를 사용하되,
  prompt는 review-only 규칙과 marker 게시 확인을 유지합니다.
- `@클로드-p`는 Claude Code subscription/OAuth 세션용 wrapper인 `claude-p`로
  실행합니다. 매 review checkout 경로를 Claude trusted project로 등록한 뒤
  wrapper가 필요한 옵션과 제한된 read/write/gh shell 명령으로 실행합니다.
  `Write`는 review dir 아래 리뷰 코멘트 markdown 작성용이고, checkout 파일
  변경은 금지합니다.
- `opencode`, `claude`, `claude-p`, `codex` 모두 marker 포함 inline review comment,
  PR review body, 또는 fallback PR comment가 실제로 게시되어야 성공입니다.

## Legacy Workflows

`.github/workflows/`와 `templates/` 아래 reusable workflow들은 과거
GitHub-hosted runner 기반 방식입니다. repo별 provider secret이 필요하고, 운영 서버의
로컬 인증을 직접 쓰지 못합니다. 새 개인 repo에는 webhook 방식을 우선 사용하세요.
