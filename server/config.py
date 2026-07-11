from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


KNOWN_AUTHOR_ASSOCIATIONS = frozenset(
    {
        "COLLABORATOR",
        "CONTRIBUTOR",
        "FIRST_TIMER",
        "FIRST_TIME_CONTRIBUTOR",
        "MANNEQUIN",
        "MEMBER",
        "NONE",
        "OWNER",
    }
)


@dataclass(frozen=True)
class Config:
    webhook_secret: str
    allowed_repositories: frozenset[str]
    allowed_author_associations: frozenset[str]
    repository_author_associations: dict[str, frozenset[str]]
    work_dir: Path
    db_path: Path
    chatgpt_browser_start_command: tuple[str, ...] = ("chatgpt-browser-start",)
    chatgpt_url: str = "https://chatgpt.com/"
    bind_host: str = "127.0.0.1"
    bind_port: int = 18080
    model_timeout_seconds: int = 2700
    marker_settle_seconds: int = 90
    posted_grace_seconds: int = 20
    poll_seconds: int = 10
    job_max_attempts: int = 4
    job_retry_delay_seconds: int = 60
    job_poll_seconds: int = 5
    job_worker_count: int = 1

    def allowed_associations_for(self, repo: str) -> frozenset[str]:
        return self.repository_author_associations.get(repo.lower(), self.allowed_author_associations)


def load_config(path: str | Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    secret = str(raw.get("webhook_secret") or "")
    if not secret or secret == "replace-with-long-random-secret":
        raise ValueError("config.webhook_secret must be set")

    repos = frozenset(str(repo).lower() for repo in raw.get("allowed_repositories", []))
    if not repos:
        raise ValueError("config.allowed_repositories must contain at least one repo")

    allowed_author_associations = _load_association_set(raw.get("allowed_author_associations", ["OWNER"]))
    per_repo_associations = {
        str(repo).lower(): _load_association_set(associations)
        for repo, associations in raw.get("repository_author_associations", {}).items()
    }

    return Config(
        webhook_secret=secret,
        allowed_repositories=repos,
        allowed_author_associations=allowed_author_associations,
        repository_author_associations=per_repo_associations,
        work_dir=Path(raw.get("work_dir", str(Path.home() / ".local/state/personal-review-machines/work"))),
        db_path=Path(raw.get("db_path", str(Path.home() / ".local/state/personal-review-machines/reviews.sqlite3"))),
        chatgpt_browser_start_command=_load_command(
            raw.get("chatgpt_browser_start_command", ["chatgpt-browser-start"]),
            "chatgpt_browser_start_command",
        ),
        chatgpt_url=str(raw.get("chatgpt_url", "https://chatgpt.com/")),
        bind_host=str(raw.get("bind_host", "127.0.0.1")),
        bind_port=int(raw.get("bind_port", 18080)),
        model_timeout_seconds=int(raw.get("model_timeout_seconds", 2700)),
        marker_settle_seconds=int(raw.get("marker_settle_seconds", 90)),
        posted_grace_seconds=int(raw.get("posted_grace_seconds", 20)),
        poll_seconds=int(raw.get("poll_seconds", 10)),
        job_max_attempts=int(raw.get("job_max_attempts", 4)),
        job_retry_delay_seconds=int(raw.get("job_retry_delay_seconds", 60)),
        job_poll_seconds=int(raw.get("job_poll_seconds", 5)),
        job_worker_count=int(raw.get("job_worker_count", 1)),
    )


def _load_association_set(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise ValueError("author association config must be a list")
    associations = frozenset(str(item).upper() for item in value if str(item).strip())
    if not associations:
        raise ValueError("author association config must contain at least one value")
    unknown = associations - KNOWN_AUTHOR_ASSOCIATIONS
    if unknown:
        raise ValueError(f"unknown author association value: {', '.join(sorted(unknown))}")
    return associations


def _load_command(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = tuple(part for part in value.split() if part)
    elif isinstance(value, list):
        parts = tuple(str(part) for part in value if str(part).strip())
    else:
        raise ValueError(f"{field_name} must be a string or list")
    if not parts:
        raise ValueError(f"{field_name} must contain at least one argument")
    return parts
