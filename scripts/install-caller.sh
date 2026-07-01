#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/install-caller.sh <repo-type> [target-workflow-path] [--force]

repo-type:
  private-mention       # reusable workflow, GitHub-hosted runner, provider secrets
  mention               # alias for private-mention
  private-claude
  private-opencode-zai
  public-trusted
  public-external

Default target path:
  .github/workflows/ai-review.yml
USAGE
}

repo_type="${1:-}"
target=".github/workflows/ai-review.yml"
force="false"

if [[ -z "$repo_type" ]]; then
  usage
  exit 2
fi
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      force="true"
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      target="$1"
      ;;
  esac
  shift
done

case "$repo_type" in
  private-mention|mention)
    template="templates/private-repo-mention-review.yml"
    ;;
  private-claude)
    template="templates/experimental-auto/private-repo-auto-review.yml"
    ;;
  private-opencode-zai)
    template="templates/experimental-auto/private-repo-opencode-zai-review.yml"
    ;;
  public-trusted)
    template="templates/experimental-auto/public-repo-trusted-pr-auto-review.yml"
    ;;
  public-external)
    template="templates/public-repo-external-pr-manual-review.yml"
    ;;
  *)
    echo "Unknown repo type: $repo_type" >&2
    usage
    exit 2
    ;;
esac

if [[ ! -f "$template" ]]; then
  echo "Template not found: $template" >&2
  exit 1
fi

if [[ -e "$target" && "$force" != "true" ]]; then
  echo "Refusing to overwrite existing workflow: $target" >&2
  echo "Re-run with --force if you really want to replace it." >&2
  exit 1
fi

mkdir -p "$(dirname "$target")"
cp "$template" "$target"

echo "Installed $template -> $target"
echo
echo "Next steps:"
echo "1. Replace YOUR_GITHUB_ID with your personal GitHub ID."
echo "2. Add ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN secret if using @claude/@클로드; CLAUDE_CODE_OAUTH_TOKEN if using @클로드-p."
echo "3. Add ZAI_API_KEY secret if using @opencode."
echo "4. Open a small test PR and comment: @claude 리뷰해줘, @클로드 리뷰해줘, @클로드-p 리뷰해줘, or @opencode 리뷰해줘"
