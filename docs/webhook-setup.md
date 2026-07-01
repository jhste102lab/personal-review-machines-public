# Webhook Setup

이 서비스는 GitHub Actions runner를 추가하지 않고 GitHub webhook으로 PR 리뷰를
시작합니다.

## GitHub Repo Settings

대상 repo마다:

1. `Settings` -> `Webhooks` -> `Add webhook`
2. Payload URL: `config.json`의 `webhook_url`
3. Content type: `application/json`
4. Secret: `config.json`의 `webhook_secret`
5. Events: `Let me select individual events` -> `Issue comments`
6. Active: enabled

public/private 모두 기본 호출자는 repo `OWNER`입니다. org repo는 repo별 설정으로
`MEMBER`/`COLLABORATOR`를 추가 허용할 수 있습니다.

CLI 설치:

```bash
scripts/install-webhook.py YOUR_GITHUB_ID/YOUR_REPO --write-config
sudo systemctl restart personal-review-machines.service
```

`--write-config`는 repo를 `config.json`의 `allowed_repositories`에 추가합니다.
`--url`을 생략하면 `config.json`의 `webhook_url`을 payload URL로 사용합니다.
이미 allowlist에 있는 repo라면 webhook만 설치하거나 중복 여부를 확인합니다.
실제 payload URL과 secret은 tracked 문서가 아니라 로컬 `config.json`에만 둡니다.
예를 들어 org repo 카나리아를 전환할 때는 실제 운영 secret을 문서에 쓰지
말고 아래처럼 allowlist shape만 확인합니다.

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

## Public Fork PRs

public repo의 fork PR도 원본 base repo에서 repo owner가 멘션하면 리뷰할 수
있습니다. 하지만 누군가 repo 자체를 fork해간 뒤 그 fork repo에서 운영 서버의
review service를 호출하는 것은 허용하지 않습니다.

이 서비스는 다음 조건을 모두 만족할 때만 리뷰를 실행합니다.

- base repo가 `allowed_repositories`에 있음
- event repository가 fork repo가 아님
- 댓글이 PR issue comment임
- 댓글 작성자의 `author_association`이 repo별 허용 목록에 있음
- 댓글이 `@오픈코드`, `@지피티높음`, `@지피티매우높음`, `@지피티확장`, `@클로드`, `@클로드-p`, `@코덱스`, `@최종리뷰`로 시작함

PR head checkout은 base repo의 `refs/pull/<number>/head`를 사용합니다. fork
repo의 secret이나 Actions 권한은 사용하지 않습니다.

## nginx Example

```nginx
location /github-webhook {
    proxy_pass http://127.0.0.1:18080/github-webhook;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Smoke Test

```bash
curl -sS http://127.0.0.1:18080/health
```

Then open a small PR in an allowed repo and comment:

```text
@오픈코드 리뷰해줘
@지피티높음 리뷰해줘
@지피티매우높음 리뷰해줘
@지피티확장 리뷰해줘
@클로드 리뷰해줘
@클로드-p 리뷰해줘
@코덱스 리뷰해줘
```
