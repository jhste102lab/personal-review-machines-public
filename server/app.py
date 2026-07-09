from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config, load_config
from .review_runner import parse_request, run_review
from .store import ReviewJob, ReviewStore


LOG = logging.getLogger("personal-review-machines")
FOLLOWUP_REVIEW_INSTRUCTIONS = frozenset({"코드리뷰", "코드 리뷰"})
FOLLOWUP_INHERIT_SECONDS = 300


class WebhookHandler(BaseHTTPRequestHandler):
    config: Config
    store: ReviewStore

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/github-webhook":
            self._send_json(404, {"error": "not_found"})
            return

        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not self._valid_signature(raw):
            self._send_json(401, {"error": "invalid_signature"})
            return

        event_name = self.headers.get("X-GitHub-Event", "")
        if event_name != "issue_comment":
            self._send_json(202, {"ignored": "event", "event": event_name})
            return

        try:
            event = json.loads(raw)
            decision = self._handle_issue_comment(event)
        except Exception as exc:
            LOG.exception("webhook handling failed")
            self._send_json(500, {"error": type(exc).__name__, "message": str(exc)})
            return

        self._send_json(202, decision)

    def _handle_issue_comment(self, event: dict) -> dict:
        if event.get("action") != "created":
            return {"ignored": "action"}
        if not event.get("issue", {}).get("pull_request"):
            return {"ignored": "not_pull_request"}

        repo = event.get("repository", {}).get("full_name", "").lower()
        if event.get("repository", {}).get("fork"):
            return {"ignored": "repository_is_fork", "repository": repo}
        if repo not in self.config.allowed_repositories:
            return {"ignored": "repository_not_allowed", "repository": repo}

        comment = event.get("comment", {})
        author_association = str(comment.get("author_association") or "").upper()
        allowed_associations = self.config.allowed_associations_for(repo)
        if author_association not in allowed_associations:
            return {
                "ignored": "author_association_not_allowed",
                "author_association": author_association,
                "allowed_author_associations": sorted(allowed_associations),
            }

        parsed = parse_request(comment.get("body") or "") or _parse_followup_request(repo, event)
        if not parsed:
            return {"ignored": "no_supported_mention"}
        engine, instruction = parsed

        comment_id = int(comment["id"])
        if not self.store.enqueue_job(repo, comment_id, engine, instruction, event):
            return {"ignored": "already_processed", "comment_id": comment_id}
        _add_comment_reaction(repo, comment_id, "eyes")
        return {"queued": True, "repository": repo, "comment_id": comment_id, "engine": engine}

    def _valid_signature(self, raw: bytes) -> bool:
        signature = self.headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(self.config.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, f"sha256={expected}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)


def serve(config_path: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(config_path)
    config.work_dir.mkdir(parents=True, exist_ok=True)
    store = ReviewStore(config.db_path)
    interrupted = store.requeue_interrupted_jobs()
    if interrupted:
        LOG.warning("requeued %s interrupted review job(s)", interrupted)

    for worker_number in range(1, config.job_worker_count + 1):
        worker = threading.Thread(
            target=_worker_loop,
            args=(config, store),
            name=f"review-worker-{worker_number}",
            daemon=True,
        )
        worker.start()

    WebhookHandler.config = config
    WebhookHandler.store = store
    server = ThreadingHTTPServer((config.bind_host, config.bind_port), WebhookHandler)
    LOG.info("listening on %s:%s", config.bind_host, config.bind_port)
    server.serve_forever()


def _worker_loop(config: Config, store: ReviewStore) -> None:
    while True:
        try:
            job = store.claim_next_job()
            if job is not None:
                _run_job(config, store, job)
        except Exception:
            LOG.exception("review worker loop failed")
        finally:
            time.sleep(config.job_poll_seconds)


def _add_comment_reaction(repo: str, comment_id: int, content: str) -> None:
    try:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{repo}/issues/comments/{comment_id}/reactions",
                "-f",
                f"content={content}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        LOG.exception("failed to add reaction repo=%s comment_id=%s", repo, comment_id)


def _parse_followup_request(repo: str, event: dict) -> tuple[str, str] | None:
    """Let a bare "코드리뷰" comment inherit the author's recent reviewer mention.

    This keeps the normal mention-only trigger posture, but supports a practical
    repeated-request flow where an allowed author posts one reviewer mention and
    then several separate "코드리뷰" comments expecting multiple runs.
    """
    comment = event.get("comment", {})
    body = str(comment.get("body") or "").strip()
    if body not in FOLLOWUP_REVIEW_INSTRUCTIONS:
        return None

    current_comment_id = int(comment.get("id") or 0)
    pr_number = int(event.get("issue", {}).get("number") or 0)
    author = str(comment.get("user", {}).get("login") or "")
    current_created_at = _parse_github_datetime(str(comment.get("created_at") or ""))
    if not current_comment_id or not pr_number or not author or current_created_at is None:
        return None

    try:
        comments = _issue_comments(repo, pr_number)
    except Exception:
        LOG.exception("failed to load issue comments for follow-up trigger repo=%s pr=%s", repo, pr_number)
        return None

    for previous in reversed(comments):
        previous_id = int(previous.get("id") or 0)
        if previous_id >= current_comment_id:
            continue
        if str(previous.get("user", {}).get("login") or "") != author:
            continue
        previous_created_at = _parse_github_datetime(str(previous.get("created_at") or ""))
        if previous_created_at is None:
            continue
        elapsed_seconds = (current_created_at - previous_created_at).total_seconds()
        if elapsed_seconds < 0:
            continue
        if elapsed_seconds > FOLLOWUP_INHERIT_SECONDS:
            return None
        parsed = parse_request(str(previous.get("body") or ""))
        if parsed:
            engine, _previous_instruction = parsed
            LOG.info(
                "inherited follow-up review trigger repo=%s pr=%s comment_id=%s engine=%s previous_comment_id=%s",
                repo,
                pr_number,
                current_comment_id,
                engine,
                previous_id,
            )
            return engine, body
    return None


def _issue_comments(repo: str, pr_number: int) -> list[dict]:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/issues/{pr_number}/comments?per_page=100",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    comments = _decode_json_arrays(result.stdout)
    return comments


def _decode_json_arrays(raw: str) -> list[dict]:
    decoder = json.JSONDecoder()
    comments: list[dict] = []
    position = 0
    while position < len(raw):
        while position < len(raw) and raw[position].isspace():
            position += 1
        if position >= len(raw):
            break
        value, position = decoder.raw_decode(raw, position)
        if not isinstance(value, list):
            raise ValueError("GitHub issue comments page was not a list")
        comments.extend(item for item in value if isinstance(item, dict))
    return comments


def _parse_github_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _run_job(config: Config, store: ReviewStore, job: ReviewJob) -> None:
    LOG.info(
        "starting review job worker=%s repo=%s comment_id=%s engine=%s attempt=%s",
        threading.current_thread().name,
        job.repository,
        job.comment_id,
        job.engine,
        job.attempts,
    )
    try:
        ok = run_review(
            config,
            job.event,
            job.engine,
            job.instruction,
            post_failure=False,
        )
    except Exception:
        LOG.exception("review job crashed repo=%s comment_id=%s", job.repository, job.comment_id)
        ok = False

    if ok:
        store.finish_job(job.repository, job.comment_id)
        LOG.info("finished review job repo=%s comment_id=%s", job.repository, job.comment_id)
        return

    message = "review marker was not posted"
    store.fail_job(job.repository, job.comment_id, message)
    LOG.error(
        "failed review job repo=%s comment_id=%s attempts=%s automatic_retry=%s",
        job.repository,
        job.comment_id,
        job.attempts,
        False,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    serve(args.config)


if __name__ == "__main__":
    main()
