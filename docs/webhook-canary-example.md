# Webhook Canary Example

## Purpose

target repo: `YOUR_ORG/YOUR_REPO`

특정 repo 하나를 첫 카나리아로 삼아 AI PR 리뷰를 reusable workflow에서 webhook
daemon 방식으로 전환할 수 있는지 검증합니다. 이 문서는
전환 전 점검 절차이며, 실제 webhook 설치, `config.json` 수정, systemd 재시작,
target repo 변경은 별도 작업에서 수행합니다.

## Preflight

운영 서버에서 daemon과 로컬 reviewer 도구 상태를 먼저 확인합니다.

```bash
curl -sS http://127.0.0.1:18080/health
gh auth status
opencode --version
claude --version
claude-p --version
codex --version
```

daemon config의 `allowed_repositories`에 target repo를 추가해야 합니다. 실제
운영 secret은 문서에 쓰지 않습니다. org repo라서
`OWNER`뿐 아니라 `MEMBER`/`COLLABORATOR`도 호출할 수 있게 repo별 override를
설정합니다.

```json
{
  "allowed_repositories": [
    "YOUR_ORG/YOUR_REPO"
  ],
  "repository_author_associations": {
    "YOUR_ORG/YOUR_REPO": [
      "OWNER",
      "MEMBER",
      "COLLABORATOR"
    ]
  }
}
```

## GitHub Webhook

target repo의 GitHub settings에서 webhook을 추가합니다.

- Payload URL: local `config.json`의 `webhook_url`
- Content type: `application/json`
- Secret: daemon config의 webhook secret
- Events: Issue comments

기존 `.github/workflows/ai-pr-review.yml`은 즉시 삭제하지 않습니다. 전환
시점에 중복 실행을 막기 위해 workflow를 비활성화하거나 트리거를 차단한 뒤
webhook daemon 동작을 검증합니다.

## Validation

1. target repo PR 댓글 첫머리에 아래 멘션을 남깁니다.

```text
@glm 리뷰해줘
```

2. 필요하면 Codex 경로도 별도 댓글로 확인합니다.

```text
@코덱스 리뷰해줘
```

3. marker 포함 댓글이 실제 PR에 게시됐는지 확인합니다.
4. 같은 comment id가 중복 실행되지 않는지 확인합니다.
5. 실패 시 실패 댓글이 게시되는지 확인합니다.
6. GitHub webhook delivery가 HTTP 202를 받는지 확인합니다.

## Success Criteria

- target repo PR에서 webhook daemon 리뷰가 1회 이상 정상 완료됩니다.
- 기존 Actions workflow와 중복 실행이 없습니다.
- 실패/타임아웃 경로가 PR 댓글로 드러납니다.
- secret, token, 실제 webhook secret 값이 노출되지 않습니다.

## Rollback

- GitHub webhook을 비활성화합니다.
- 기존 workflow를 재활성화합니다.
- daemon allowlist에서 repo를 제거하거나 daemon을 재시작합니다.
