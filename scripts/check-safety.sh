#!/usr/bin/env bash
set -euo pipefail

failures=0

fail() {
  echo "FAIL: $1"
  failures=$((failures + 1))
}

pass() {
  echo "PASS: $1"
}

scan_paths=(".github" "templates" "prompts" "docs" "scripts" "server" "README.md" "config.example.json")

if grep -RIEon --exclude-dir=.git '(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|zai-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{30,})' "${scan_paths[@]}" >/tmp/check-safety-secrets-raw.txt 2>/dev/null &&
  grep -Ev 'zai-(builtin|coding-plan|default|raw)[A-Za-z0-9_-]*' /tmp/check-safety-secrets-raw.txt >/tmp/check-safety-secrets.txt; then
  fail "real-looking API key or token found"
  cat /tmp/check-safety-secrets.txt
else
  pass "no real-looking API keys found"
fi

if grep -RIn "github.event.comment.author_association == 'OWNER'" templates .github >/tmp/check-safety-owner-workflow.txt 2>/dev/null; then
  pass "legacy workflow surfaces with mention triggers include owner checks"
else
  pass "no direct workflow owner-only check required for webhook service"
fi

if grep -q "allowed_associations_for(repo)" server/app.py &&
  grep -q "allowed_author_associations" server/config.py &&
  grep -q "repository_author_associations" server/config.py &&
  grep -q '"allowed_author_associations"' config.example.json; then
  pass "webhook service enforces author-association allowlists"
else
  fail "webhook service must restrict issue_comment triggers by author-association allowlist"
fi

if grep -q "repository_is_fork" server/app.py && grep -q "get(\"fork\")" server/app.py; then
  pass "webhook service rejects events from fork repositories"
else
  fail "webhook service must reject events from fork repositories"
fi

if grep -q -- "--dangerously-bypass-approvals-and-sandbox" server/review_runner.py; then
  fail "review runner must not use broad sandbox bypass"
else
  pass "review runner avoids broad sandbox bypass"
fi

if grep -q -- "--sandbox" server/review_runner.py && grep -q "danger-full-access" server/review_runner.py; then
  pass "codex runner allows GitHub API access for marker posting"
else
  fail "codex runner must allow GitHub API access for marker posting"
fi

if grep -q "PR code 실행, build, test, install 금지" server/review_runner.py; then
  pass "review prompt forbids PR code execution/build/test/install"
else
  fail "review prompt must forbid PR code execution/build/test/install"
fi

if grep -q "X-Hub-Signature-256" server/app.py && grep -q "hmac.compare_digest" server/app.py; then
  pass "webhook service verifies GitHub HMAC signature"
else
  fail "webhook service must verify GitHub HMAC signature"
fi

if grep -q "allowed_repositories" server/app.py && grep -q "allowed_repositories" config.example.json; then
  pass "webhook service has repository allowlist"
else
  fail "webhook service must enforce repository allowlist"
fi

if grep -RIn "contents:[[:space:]]*write" .github templates >/tmp/check-safety-contents-write.txt 2>/dev/null; then
  fail "contents write permission found in legacy workflows/templates"
  cat /tmp/check-safety-contents-write.txt
else
  pass "no contents write permission in legacy workflows/templates"
fi

if grep -RIn "actions/cache" .github templates >/tmp/check-safety-cache.txt 2>/dev/null; then
  fail "GitHub Actions cache usage found"
  cat /tmp/check-safety-cache.txt
else
  pass "no GitHub Actions cache usage"
fi

echo
echo "Manual inspection commands:"
echo 'grep -R "author_association" server .github templates'
echo 'grep -R "allowed_author_associations\|repository_author_associations" server config.example.json'
echo 'grep -R "X-Hub-Signature" server'
echo 'grep -R "GITHUB_TOKEN" .github templates server'

if [[ "$failures" -gt 0 ]]; then
  echo
  echo "Safety check failed with $failures issue(s)."
  exit 1
fi

echo
echo "Safety check passed."
