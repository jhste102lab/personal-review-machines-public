#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPECTED_EVENT = "issue_comment"


@dataclass(frozen=True)
class ServiceConfig:
    webhook_url: str
    webhook_secret: str
    allowed_repositories: tuple[str, ...]


@dataclass(frozen=True)
class SyncResult:
    repository: str
    hook_id: int | None
    status: str
    changed: bool = False
    ping_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and (self.ping_code in {None, 202})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check or repair GitHub webhooks for Personal Review Machines allowed repositories."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Service config containing webhook_url, webhook_secret, and allowed_repositories.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit sync to one repository. May be passed multiple times. Defaults to all allowed repositories.",
    )
    parser.add_argument("--fix", action="store_true", help="Create or update drifted webhooks.")
    parser.add_argument("--ping", action="store_true", help="Ping each in-sync webhook and verify last_response is 202.")
    parser.add_argument(
        "--ping-wait-seconds",
        type=float,
        default=5.0,
        help="Seconds to wait after webhook pings before reading last_response.",
    )
    parser.add_argument("--gh-bin", default="gh", help="GitHub CLI binary to execute.")
    args = parser.parse_args()

    try:
        config = load_service_config(Path(args.config))
        repositories = selected_repositories(config.allowed_repositories, args.repo)
        results = [sync_repository(args.gh_bin, config, repo, fix=args.fix) for repo in repositories]
        if args.ping:
            results = ping_synced_hooks(args.gh_bin, results, args.ping_wait_seconds)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for result in results:
        print(format_result(result))

    if all(result.ok for result in results):
        return 0
    return 1


def load_service_config(path: Path) -> ServiceConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    webhook_url = str(raw.get("webhook_url") or "").strip()
    webhook_secret = str(raw.get("webhook_secret") or "")
    repositories = tuple(str(repo).strip() for repo in raw.get("allowed_repositories", []) if str(repo).strip())

    if not webhook_url:
        raise ValueError("config.webhook_url must be set")
    if not webhook_secret or webhook_secret == "replace-with-long-random-secret":
        raise ValueError("config.webhook_secret must be set")
    if not repositories:
        raise ValueError("config.allowed_repositories must contain at least one repo")
    return ServiceConfig(webhook_url=webhook_url, webhook_secret=webhook_secret, allowed_repositories=repositories)


def selected_repositories(allowed_repositories: tuple[str, ...], requested: list[str]) -> tuple[str, ...]:
    if not requested:
        return allowed_repositories
    allowed_by_lower = {repo.lower(): repo for repo in allowed_repositories}
    selected: list[str] = []
    for repo in requested:
        repo_key = repo.strip().lower()
        if repo_key not in allowed_by_lower:
            raise ValueError(f"{repo} is not in config.allowed_repositories")
        selected.append(allowed_by_lower[repo_key])
    return tuple(selected)


def sync_repository(gh_bin: str, config: ServiceConfig, repo: str, *, fix: bool) -> SyncResult:
    hooks = gh_json(gh_bin, ["api", f"repos/{repo}/hooks"])
    hook = choose_managed_hook(hooks, config.webhook_url)
    if hook is None:
        if not fix:
            return SyncResult(repo, None, "drift", detail="missing issue_comment webhook")
        created = create_hook(gh_bin, repo, config)
        return SyncResult(repo, int(created["id"]), "ok", changed=True, detail="created")

    hook_id = int(hook["id"])
    drift_reasons = drift_reasons_for(hook, config.webhook_url)
    if not drift_reasons:
        return SyncResult(repo, hook_id, "ok")
    if not fix:
        return SyncResult(repo, hook_id, "drift", detail=", ".join(drift_reasons))

    updated = update_hook(gh_bin, repo, hook_id, config)
    return SyncResult(repo, int(updated["id"]), "ok", changed=True, detail="updated: " + ", ".join(drift_reasons))


def choose_managed_hook(hooks: Any, webhook_url: str) -> dict[str, Any] | None:
    if not isinstance(hooks, list):
        raise ValueError("GitHub hooks response was not a list")
    for hook in hooks:
        if hook.get("name") == "web" and hook.get("config", {}).get("url") == webhook_url:
            return hook
    for hook in hooks:
        if hook.get("name") == "web" and EXPECTED_EVENT in hook.get("events", []):
            return hook
    return None


def drift_reasons_for(hook: dict[str, Any], webhook_url: str) -> list[str]:
    reasons: list[str] = []
    config = hook.get("config", {})
    if not hook.get("active"):
        reasons.append("inactive")
    if hook.get("name") != "web":
        reasons.append("not web hook")
    if EXPECTED_EVENT not in hook.get("events", []):
        reasons.append(f"missing {EXPECTED_EVENT} event")
    if config.get("url") != webhook_url:
        reasons.append("url mismatch")
    if config.get("content_type") != "json":
        reasons.append("content_type mismatch")
    return reasons


def create_hook(gh_bin: str, repo: str, config: ServiceConfig) -> dict[str, Any]:
    payload = hook_payload(config)
    return gh_json_with_payload(gh_bin, ["api", "--method", "POST", f"repos/{repo}/hooks"], payload)


def update_hook(gh_bin: str, repo: str, hook_id: int, config: ServiceConfig) -> dict[str, Any]:
    payload = hook_payload(config)
    return gh_json_with_payload(gh_bin, ["api", "--method", "PATCH", f"repos/{repo}/hooks/{hook_id}"], payload)


def hook_payload(config: ServiceConfig) -> dict[str, Any]:
    return {
        "name": "web",
        "active": True,
        "events": [EXPECTED_EVENT],
        "config": {
            "url": config.webhook_url,
            "content_type": "json",
            "secret": config.webhook_secret,
            "insecure_ssl": "0",
        },
    }


def ping_synced_hooks(gh_bin: str, results: list[SyncResult], wait_seconds: float) -> list[SyncResult]:
    pingable = [result for result in results if result.status == "ok" and result.hook_id is not None]
    for result in pingable:
        gh_json(gh_bin, ["api", "--method", "POST", f"repos/{result.repository}/hooks/{result.hook_id}/pings"], allow_empty=True)
    if pingable and wait_seconds > 0:
        time.sleep(wait_seconds)

    checked: list[SyncResult] = []
    pingable_by_key = {(result.repository, result.hook_id) for result in pingable}
    for result in results:
        if (result.repository, result.hook_id) not in pingable_by_key:
            checked.append(result)
            continue
        hook = gh_json(gh_bin, ["api", f"repos/{result.repository}/hooks/{result.hook_id}"])
        last_response = hook.get("last_response") or {}
        code = last_response.get("code")
        detail = result.detail
        if code != 202:
            message = str(last_response.get("message") or "ping did not return 202")
            detail = append_detail(detail, message)
        checked.append(
            SyncResult(
                result.repository,
                result.hook_id,
                result.status,
                changed=result.changed,
                ping_code=code if isinstance(code, int) else None,
                detail=detail,
            )
        )
    return checked


def append_detail(existing: str, extra: str) -> str:
    if not existing:
        return extra
    return f"{existing}; {extra}"


def format_result(result: SyncResult) -> str:
    fields = [
        f"repo={result.repository}",
        f"hook_id={result.hook_id if result.hook_id is not None else '-'}",
        f"status={result.status}",
        f"changed={'yes' if result.changed else 'no'}",
    ]
    if result.ping_code is not None:
        fields.append(f"ping_code={result.ping_code}")
    if result.detail:
        fields.append(f"detail={result.detail}")
    return " ".join(fields)


def gh_json(gh_bin: str, args: list[str], *, allow_empty: bool = False) -> Any:
    result = subprocess.run([gh_bin, *args], check=True, capture_output=True, text=True)
    if allow_empty and not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def gh_json_with_payload(gh_bin: str, args: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        json.dump(payload, tmp)
        tmp_path = Path(tmp.name)
    try:
        tmp_path.chmod(0o600)
        response = gh_json(gh_bin, [*args, "--input", str(tmp_path)])
        if not isinstance(response, dict):
            raise ValueError("GitHub hook response was not an object")
        return response
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
