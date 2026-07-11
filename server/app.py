from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config, load_config
from .review_runner import CHATGPT_ENGINES, ReviewOutcome, parse_request, run_review
from .store import ReviewJob, ReviewStore


LOG = logging.getLogger("personal-review-machines")


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

        parsed = parse_request(comment.get("body") or "")
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
    requeued, failed = store.recover_interrupted_jobs(CHATGPT_ENGINES)
    if requeued:
        LOG.warning("requeued %s interrupted review job(s)", requeued)
    if failed:
        LOG.error(
            "marked %s interrupted ChatGPT job(s) failed; delivery was uncertain and they were not retried",
            failed,
        )

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
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("timed out adding reaction repo=%s comment_id=%s", repo, comment_id)
    except Exception:
        LOG.exception("failed to add reaction repo=%s comment_id=%s", repo, comment_id)


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
        outcome = run_review(
            config,
            job.event,
            job.engine,
            job.instruction,
            post_failure=False,
        )
    except Exception:
        LOG.exception("review job crashed repo=%s comment_id=%s", job.repository, job.comment_id)
        # A crash can happen after a browser process submitted the prompt but
        # before the marker was observed. Never retry ChatGPT in that state.
        outcome = ReviewOutcome(
            False,
            retryable=job.engine not in CHATGPT_ENGINES,
            reason="worker_exception_delivery_uncertain" if job.engine in CHATGPT_ENGINES else "worker_exception",
        )

    if outcome.success:
        store.finish_job(job.repository, job.comment_id)
        LOG.info("finished review job repo=%s comment_id=%s", job.repository, job.comment_id)
        return

    message = outcome.reason or "review marker was not posted"
    if outcome.retryable and job.attempts < config.job_max_attempts:
        delay_seconds = max(0, config.job_retry_delay_seconds) * (2 ** max(0, job.attempts - 1))
        store.retry_job(job.repository, job.comment_id, delay_seconds, message)
        LOG.warning(
            "retrying review job repo=%s comment_id=%s attempt=%s next_attempt=%s delay_seconds=%s",
            job.repository,
            job.comment_id,
            job.attempts,
            job.attempts + 1,
            delay_seconds,
        )
        return

    store.fail_job(job.repository, job.comment_id, message)
    if not outcome.retryable:
        LOG.error(
            "not retrying review job repo=%s comment_id=%s reason=%s; delivery may already have occurred",
            job.repository,
            job.comment_id,
            message,
        )
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
