# Repository Guidelines

## Project Structure & Module Organization

This repository runs a personal GitHub PR review webhook service. Core service code lives in `server/`: `app.py` receives GitHub webhooks, `config.py` loads JSON settings, `review_runner.py` checks out PRs and launches local review tools, and `store.py` tracks processed comments in SQLite. Markdown review prompts are in `prompts/`. Documentation is in `docs/`. Deployment assets live in `ops/`, including systemd and nginx examples. `templates/` and `.github/workflows/` contain legacy GitHub Actions review workflows; keep them compatible, but prefer the webhook daemon for new integrations.

## Build, Test, and Development Commands

- `python3 -m server.app --config config.json`: run the webhook server locally using the real local config.
- `python3 -m compileall server scripts`: quick syntax/import smoke check for Python files.
- `scripts/check-safety.sh`: scan for token leaks and verify key security invariants.
- `scripts/install-webhook.py OWNER/REPO --write-config`: register a repository webhook and update allowed repositories.
- `sudo systemctl restart personal-review-machines.service`: restart the deployed daemon after service-impacting changes.

## Coding Style & Naming Conventions

Use Python 3 stdlib-first code unless a dependency is already established. Keep four-space indentation, type annotations, and small focused functions. Use `snake_case` for Python variables/functions and lowercase engine identifiers such as `opencode`, `claude_p`, and `codexcli`. Prefer explicit paths and subprocess argument lists over shell strings. Prompts should remain clear, direct, and review-only.

## Testing Guidelines

There is no formal test suite yet. Before opening a PR, run `python3 -m compileall server scripts` and `scripts/check-safety.sh`. For webhook behavior changes, manually exercise `/health` and a representative `issue_comment` payload when practical. Security-sensitive changes must preserve HMAC verification, repository allowlisting, author-association allowlisting, fork-repo rejection, read-only review execution, and marker-based completion checks.

## Commit & Pull Request Guidelines

Recent commits use short imperative summaries, for example `Harden webhook reviews against fork repo invocation` and `Run claude-p reviewer with isolated system prompt`. Keep commits narrow and describe the behavior changed, not just the file touched. PRs should include a concise summary, validation commands run, security implications, and any deployment steps such as service restart or webhook reconfiguration. Link related issues when available.

## Security & Configuration Tips

Never commit `config.json`, secrets, API keys, review work directories, or SQLite state. Use `config.example.json` for documented defaults. Treat `ops/` files as deployment references and check host-specific paths before copying them into production.
