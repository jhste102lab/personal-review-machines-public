#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or update a Personal Review Machines GitHub webhook for one repo."
    )
    parser.add_argument("repository", help="Repository full name, e.g. YOUR_GITHUB_ID/YOUR_REPO")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Local service config containing webhook_secret and allowed_repositories",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Webhook payload URL. Defaults to config.webhook_url when set.",
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="Add the repository to allowed_repositories in config.json when missing",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    webhook_url = args.url or config.get("webhook_url")
    if not webhook_url:
        print(
            "Webhook payload URL is required. Pass --url or set webhook_url in config.json.",
            file=sys.stderr,
        )
        return 2

    repo = args.repository.strip()
    repo_lc = repo.lower()

    allowed = [str(item) for item in config.get("allowed_repositories", [])]
    if repo_lc not in {item.lower() for item in allowed}:
        if not args.write_config:
            print(
                f"{repo} is not in config.allowed_repositories. "
                "Re-run with --write-config to add it.",
                file=sys.stderr,
            )
            return 2
        allowed.append(repo)
        config["allowed_repositories"] = allowed
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        config_path.chmod(0o600)

    existing = gh_json(["api", f"repos/{repo}/hooks"])
    for hook in existing:
        if hook.get("config", {}).get("url") == webhook_url:
            print(f"Webhook already exists for {repo}: id={hook['id']} url={webhook_url}")
            return 0

    payload = {
        "name": "web",
        "active": True,
        "events": ["issue_comment"],
        "config": {
            "url": webhook_url,
            "content_type": "json",
            "secret": config["webhook_secret"],
            "insecure_ssl": "0",
        },
    }
    payload_path = Path("/tmp/personal-review-machines-webhook.json")
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        created = gh_json(["api", "--method", "POST", f"repos/{repo}/hooks", "--input", str(payload_path)])
    finally:
        payload_path.unlink(missing_ok=True)

    print(f"Installed webhook for {repo}: id={created['id']} url={created['config']['url']}")
    return 0


def gh_json(args: list[str]) -> object:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
