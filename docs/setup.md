# Setup

기본 리뷰 흐름은 PR 자동 실행이 아니라 mention-triggered 실행입니다. PR을 열거나 synchronize할 때는 Actions minutes와 model credit을 쓰지 않고, PR 댓글에서 직접 요청할 때만 실행합니다.

## Current webhook daemon setup

현재 권장 경로는 reusable workflow가 아니라 webhook daemon입니다. 대상 repo에는
provider secret이나 caller workflow를 먼저 넣지 않고, GitHub webhook을
`/github-webhook` endpoint로 연결합니다.

1. daemon 설정 파일을 준비합니다.

```bash
cp config.example.json config.json
```

`config.json`은 `.gitignore`에 포함된 로컬 운영 파일입니다. 실제 webhook URL,
webhook secret, 운영 repo allowlist는 이 파일에만 저장하고 public repo에 commit하지
않습니다.

2. `config.json`에 webhook URL, 긴 임의 webhook secret, 허용 repo allowlist를 설정합니다.

```json
{
  "webhook_secret": "replace-with-long-random-secret",
  "webhook_url": "https://review.example.com/github-webhook",
  "allowed_repositories": ["YOUR_GITHUB_ID/YOUR_REPO"],
  "allowed_author_associations": ["OWNER"]
}
```

org repo에서 repo owner가 아닌 멤버/협업자가 리뷰를 호출해야 한다면 repo별 override를
추가합니다.

```json
{
  "repository_author_associations": {
    "YOUR_ORG/YOUR_REPO": ["OWNER", "MEMBER", "COLLABORATOR"]
  }
}
```

3. 대상 repo에 webhook을 설치합니다. `scripts/install-webhook.py`는 `--url`이 없으면
`config.json`의 `webhook_url`을 사용합니다.

```bash
scripts/install-webhook.py YOUR_GITHUB_ID/YOUR_REPO --write-config
sudo systemctl restart personal-review-machines.service
```

수동 설치 값은 `docs/webhook-setup.md`를 따릅니다. 운영 서버에서는 webhook URL/secret
drift를 막기 위해 30분 주기의 sync timer도 같이 켭니다.

```bash
sudo install -m 644 ops/systemd/personal-review-machines-webhook-sync.service /etc/systemd/system/personal-review-machines-webhook-sync.service
sudo install -m 644 ops/systemd/personal-review-machines-webhook-sync.timer /etc/systemd/system/personal-review-machines-webhook-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now personal-review-machines-webhook-sync.timer
```

수동으로 즉시 맞추려면 아래 명령을 실행합니다. secret 값은 출력하지 않습니다.

```bash
scripts/sync-webhooks.py --config /etc/personal-review-machines/config.json --fix --ping
```

4. 운영 서버의 로컬 인증과 reviewer CLI를 확인합니다.

```bash
sudo apt-get update
sudo apt-get install -y xvfb x11vnc novnc websockify chromium-browser || sudo apt-get install -y xvfb x11vnc novnc websockify chromium
curl -sS http://127.0.0.1:18080/health
gh auth status
opencode --version
scripts/chatgpt-browser-status
claude --version
claude-p --version
codex --version
```

ChatGPT reviewer를 쓸 경우에는 브라우저 helper도 한 번 준비합니다.

```bash
scripts/chatgpt-browser-start
scripts/chatgpt-browser-status
```

처음 한 번은 VNC/noVNC로 접속해서 ChatGPT 로그인과 GitHub 연동을 직접 완료해야 합니다.
기본 포트는 localhost 바인딩 기준 VNC `5901`, noVNC `6080`입니다.

```bash
ssh -L 6080:127.0.0.1:6080 USER@HOST
# then open http://127.0.0.1:6080/vnc.html
```

5. allowed repo의 PR 댓글 첫머리에 멘션을 남기고 marker 포함 댓글이 실제 PR에
게시되는지 확인합니다.

```text
@glm 리뷰해줘
@미니맥스 리뷰해줘
@딥시크 리뷰해줘
@지피티높음 리뷰해줘
@지피티매우높음 리뷰해줘
@지피티확장 리뷰해줘
@클로드 리뷰해줘
@클로드-p 리뷰해줘
@코덱스 리뷰해줘
```

systemd로 부팅 시 브라우저 helper까지 올리려면 아래 서비스도 함께 설치합니다.

```bash
sudo install -m 644 ops/systemd/personal-review-machines-chatgpt-browser.service /etc/systemd/system/personal-review-machines-chatgpt-browser.service
sudo systemctl daemon-reload
sudo systemctl enable --now personal-review-machines-chatgpt-browser.service
```

## Legacy reusable workflow setup

아래 절차는 `.github/workflows/`와 `templates/`에 남아 있는 과거 reusable workflow
경로입니다. 새 repo 연동에는 webhook daemon 방식을 우선 사용하세요.

## Legacy target repo secrets

`@claude`/`@클로드`를 쓸 repo:

```bash
gh secret set ANTHROPIC_API_KEY --repo YOUR_GITHUB_ID/YOUR_REPO --body "$ANTHROPIC_API_KEY"
# 또는 Claude Code subscription/OAuth token을 쓸 때:
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo YOUR_GITHUB_ID/YOUR_REPO --body "$CLAUDE_CODE_OAUTH_TOKEN"
```

`@클로드-p`를 쓸 repo:

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo YOUR_GITHUB_ID/YOUR_REPO --body "$CLAUDE_CODE_OAUTH_TOKEN"
```

`@opencode`를 쓸 repo:

```bash
gh secret set ZAI_API_KEY --repo YOUR_GITHUB_ID/YOUR_REPO --body "$ZAI_API_KEY"
```

여러 reviewer를 쓸 repo에는 필요한 reviewer secret을 모두 넣습니다. `@claude`/`@클로드`는 `ANTHROPIC_API_KEY` 또는 `CLAUDE_CODE_OAUTH_TOKEN` 중 하나가 필요하고, `@클로드-p`는 `CLAUDE_CODE_OAUTH_TOKEN`이 필요합니다. 하나만 쓸 repo에는 필요한 secret만 넣으면 됩니다.

중앙 repo의 secret은 reusable workflow를 호출하는 repo에 자동으로 제공되지 않습니다. 각 대상 repo에 직접 secret을 저장해야 합니다.

## Legacy caller workflow 설치

권장 기본 템플릿:

```text
templates/private-repo-mention-review.yml
```

대상 repo에 복사할 위치:

```text
.github/workflows/ai-review.yml
```

복사 후 `YOUR_GITHUB_ID/personal-review-machines`를 실제 중앙 repo 경로로 바꿉니다.

## Legacy helper script 사용

중앙 repo에서 대상 repo 작업 디렉터리로 템플릿을 복사할 때:

```bash
scripts/install-caller.sh private-mention
scripts/install-caller.sh private-mention .github/workflows/ai-review.yml --force
```

자동 `pull_request` 리뷰 템플릿은 `templates/experimental-auto/` 아래에 있습니다. 기본 운영에는 쓰지 마세요.

## Legacy workflow 테스트

1. 대상 repo에서 작은 테스트 PR을 엽니다.
2. PR이 열렸을 때 자동 리뷰가 돌지 않는지 확인합니다.
3. PR 댓글에 `@claude 리뷰해줘`, `@클로드 리뷰해줘`, `@클로드-p 리뷰해줘`, 또는 `@opencode 리뷰해줘`를 남깁니다.
4. Actions 로그에서 runner가 `ubuntu-latest` 또는 `ubuntu-24.04`인지 확인합니다.
5. 선택한 reviewer의 CLI가 설치되는지 확인합니다.
6. 리뷰 코멘트가 한국어로 올라오는지 확인합니다.

Claude OAuth/subscription token을 테스트할 때는 `Run Claude Code review` step이 `Not logged in · Please run /login`으로 끝나지 않아야 합니다. 이 오류가 나면 reusable workflow가 `CLAUDE_CODE_OAUTH_TOKEN`을 받지 못했거나, Claude Code가 OAuth를 읽지 않는 `--bare` 모드로 실행되고 있는지 확인합니다.

OpenCode를 테스트할 때 PR comment가 `sqlite-migration:done` 같은 migration 로그만 담고 끝나면 fresh runner 첫 실행이 review 전에 종료된 것입니다. mention workflow는 이 로그를 감지해 한 번 재시도하므로, 최신 중앙 workflow를 타고 있는지 확인합니다.

## PR head checkout

Mention review는 매칭된 요청에 대해 PR head를 `actions/checkout`으로 `pr-head/`에 checkout합니다. reviewer는 diff/context뿐 아니라 직접 관련된 코드, 테스트, schema, migration, config, API contract를 읽기 전용으로 탐색할 수 있습니다.

테스트, 빌드, package manager, formatter, generator, network, write/edit/delete, commit, push, destructive command는 금지합니다.

## Legacy GitHub-hosted runner 확인

Actions job 로그의 `Set up job` 섹션에서 runner image를 확인합니다. 이 확인은
legacy reusable workflow 경로에만 해당합니다.

## Legacy Actions 사용량 확인

개인 계정 GitHub Pro의 Actions minute 사용량은 GitHub Settings의 Billing and plans 영역에서 확인합니다.

## Legacy 여러 repo에 secret 넣기

```bash
for repo in repo-a repo-b repo-c; do
  gh secret set ANTHROPIC_API_KEY --repo YOUR_GITHUB_ID/$repo --body "$ANTHROPIC_API_KEY"
  gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo YOUR_GITHUB_ID/$repo --body "$CLAUDE_CODE_OAUTH_TOKEN"
  gh secret set ZAI_API_KEY --repo YOUR_GITHUB_ID/$repo --body "$ZAI_API_KEY"
done
```
