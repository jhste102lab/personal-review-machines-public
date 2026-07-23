from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hmac
import hashlib
import json
import logging
import random
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config, load_config
from .review_runner import CHATGPT_ENGINES, ReviewOutcome, parse_request, run_review
from .store import ReviewJob, ReviewStore


LOG = logging.getLogger("personal-review-machines")

CHATGPT_PRE_SEND_RETRY_REASONS = frozenset(
    {
        "chatgpt_connection_failed_before_prompt",
        "chatgpt_prompt_send_failed",
        "pre_send_failure",
    }
)


class LaunchGate:
    """Stagger non-ChatGPT reviewer process starts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_start_at = 0.0

    def wait_for_turn(self, min_seconds: int, max_seconds: int) -> None:
        interval = random.uniform(min_seconds, max(max_seconds, min_seconds))
        while True:
            with self._lock:
                now = time.monotonic()
                wait_seconds = max(0.0, self._next_start_at - now)
                if wait_seconds == 0:
                    self._next_start_at = now + interval
                    return
            time.sleep(wait_seconds)


class ChatGPTSendGate:
    """Serialize same-account ChatGPT sends and persist their cooldown."""

    def __init__(
        self,
        store: ReviewStore,
        config: Config,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        random_interval: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._random_interval = random_interval
        self._lock = threading.Lock()

    @contextmanager
    def wait_for_turn(self) -> Iterator[None]:
        """Hold the global send lane until the caller records its outcome."""
        with self._lock:
            next_send_at = self._store.chatgpt_next_send_at()
            delay_seconds = max(0.0, next_send_at - self._clock())
            if delay_seconds > 0:
                LOG.info("waiting for global ChatGPT send cooldown delay_seconds=%.1f", delay_seconds)
                self._sleep(delay_seconds)
            yield

    def defer_after_success(self) -> float:
        delay_seconds = self._success_gap_seconds()
        self._defer(delay_seconds, "session_created")
        return delay_seconds

    def defer_after_pre_send_failure(self, delay_seconds: float) -> None:
        self._defer(delay_seconds, "pre_send_failure")

    def defer_after_delivery_uncertain(self) -> float:
        delay_seconds = self._success_gap_seconds()
        self._defer(delay_seconds, "delivery_uncertain")
        return delay_seconds

    def _success_gap_seconds(self) -> float:
        return self._random_interval(
            self._config.chatgpt_success_gap_min_seconds,
            self._config.chatgpt_success_gap_max_seconds,
        )

    def _defer(self, delay_seconds: float, reason: str) -> None:
        delay_seconds = max(0.0, delay_seconds)
        self._store.defer_chatgpt_send_until(self._clock() + delay_seconds)
        LOG.info(
            "updated global ChatGPT send cooldown reason=%s delay_seconds=%.1f",
            reason,
            delay_seconds,
        )


class WebhookHandler(BaseHTTPRequestHandler):
    config: Config
    store: ReviewStore
    launch_gate: LaunchGate
    chatgpt_send_gate: ChatGPTSendGate

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
        _schedule_job(self.config, self.store, self.launch_gate, self.chatgpt_send_gate, repo, comment_id)
        _add_comment_reaction(repo, comment_id, "eyes")
        return {"accepted": True, "repository": repo, "comment_id": comment_id, "engine": engine}

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

    launch_gate = LaunchGate()
    chatgpt_send_gate = ChatGPTSendGate(store, config)
    WebhookHandler.config = config
    WebhookHandler.store = store
    WebhookHandler.launch_gate = launch_gate
    WebhookHandler.chatgpt_send_gate = chatgpt_send_gate
    server = ThreadingHTTPServer((config.bind_host, config.bind_port), WebhookHandler)
    pending_jobs = store.list_queued_jobs()
    for job, delay_seconds in pending_jobs:
        _schedule_job(
            config,
            store,
            launch_gate,
            chatgpt_send_gate,
            job.repository,
            job.comment_id,
            delay_seconds,
        )
    LOG.info(
        "listening on %s:%s; dispatch interval=%ss chatgpt_success_gap=%s-%ss pending_jobs=%s",
        config.bind_host,
        config.bind_port,
        config.job_start_interval_seconds,
        config.chatgpt_success_gap_min_seconds,
        config.chatgpt_success_gap_max_seconds,
        len(pending_jobs),
    )
    server.serve_forever()


def _schedule_job(
    config: Config,
    store: ReviewStore,
    launch_gate: LaunchGate,
    chatgpt_send_gate: ChatGPTSendGate,
    repository: str,
    comment_id: int,
    delay_seconds: float = 0,
) -> None:
    if delay_seconds > 0:
        timer = threading.Timer(
            delay_seconds,
            _schedule_job,
            args=(config, store, launch_gate, chatgpt_send_gate, repository, comment_id),
        )
        timer.daemon = True
        timer.start()
        return
    thread = threading.Thread(
        target=_dispatch_job,
        args=(config, store, launch_gate, chatgpt_send_gate, repository, comment_id),
        name=f"review-dispatch-{comment_id}",
        daemon=True,
    )
    thread.start()


def _dispatch_job(
    config: Config,
    store: ReviewStore,
    launch_gate: LaunchGate,
    chatgpt_send_gate: ChatGPTSendGate,
    repository: str,
    comment_id: int,
) -> None:
    try:
        engine = store.queued_job_engine(repository, comment_id)
        if engine is None:
            LOG.info("skipping job repo=%s comment_id=%s; it is no longer due", repository, comment_id)
            return
        if engine in CHATGPT_ENGINES:
            with chatgpt_send_gate.wait_for_turn():
                job = store.start_job(repository, comment_id)
                if job is None:
                    LOG.info("skipping job repo=%s comment_id=%s; it is no longer due", repository, comment_id)
                    return
                _run_job(config, store, launch_gate, chatgpt_send_gate, job)
            return

        job = store.start_job(repository, comment_id)
        if job is None:
            LOG.info("skipping job repo=%s comment_id=%s; it is no longer due", repository, comment_id)
            return
        launch_gate.wait_for_turn(config.job_start_interval_seconds, config.job_start_interval_seconds)
        _run_job(config, store, launch_gate, chatgpt_send_gate, job)
    except Exception:
        LOG.exception("review dispatch failed repo=%s comment_id=%s", repository, comment_id)
        store.fail_job(repository, comment_id, "review dispatch failed")


def _add_comment_reaction(repo: str, comment_id: int, content: str) -> None:
    try:
        result = subprocess.run(
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
        if result.returncode != 0:
            LOG.warning(
                "failed to add reaction repo=%s comment_id=%s content=%s exit_code=%s",
                repo,
                comment_id,
                content,
                result.returncode,
            )
    except subprocess.TimeoutExpired:
        LOG.warning("timed out adding reaction repo=%s comment_id=%s", repo, comment_id)
    except Exception:
        LOG.exception("failed to add reaction repo=%s comment_id=%s", repo, comment_id)


def _run_job(
    config: Config,
    store: ReviewStore,
    launch_gate: LaunchGate,
    chatgpt_send_gate: ChatGPTSendGate,
    job: ReviewJob,
) -> None:
    LOG.info(
        "starting review job thread=%s repo=%s comment_id=%s engine=%s attempt=%s",
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
        if job.engine in CHATGPT_ENGINES and outcome.reason in {
            "chatgpt_session_created",
            "marker_posted",
            "marker_already_posted",
        }:
            cooldown_seconds = chatgpt_send_gate.defer_after_success()
            _add_comment_reaction(job.repository, job.comment_id, "rocket")
            LOG.info(
                "ChatGPT chat session sent; next global send is delayed repo=%s comment_id=%s cooldown_seconds=%.1f",
                job.repository,
                job.comment_id,
                cooldown_seconds,
            )
        LOG.info("finished review job repo=%s comment_id=%s", job.repository, job.comment_id)
        return

    message = outcome.reason or "review marker was not posted"
    retry_delay_seconds: int | None = None
    if job.engine in CHATGPT_ENGINES:
        if outcome.reason in CHATGPT_PRE_SEND_RETRY_REASONS:
            retry_delay_seconds = _retry_delay_seconds(config, job, outcome)
            chatgpt_send_gate.defer_after_pre_send_failure(retry_delay_seconds)
        else:
            cooldown_seconds = chatgpt_send_gate.defer_after_delivery_uncertain()
            LOG.warning(
                "ChatGPT delivery is uncertain; delaying the next global send repo=%s comment_id=%s cooldown_seconds=%.1f",
                job.repository,
                job.comment_id,
                cooldown_seconds,
            )
    if outcome.retryable and job.attempts < config.job_max_attempts:
        delay_seconds = retry_delay_seconds if retry_delay_seconds is not None else _retry_delay_seconds(config, job, outcome)
        store.retry_job(job.repository, job.comment_id, delay_seconds, message)
        _schedule_job(
            config,
            store,
            launch_gate,
            chatgpt_send_gate,
            job.repository,
            job.comment_id,
            delay_seconds,
        )
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


def _retry_delay_seconds(config: Config, job: ReviewJob, outcome: ReviewOutcome) -> int:
    if job.engine in CHATGPT_ENGINES and outcome.reason in CHATGPT_PRE_SEND_RETRY_REASONS:
        delays = config.chatgpt_pre_send_retry_delays_seconds
        index = min(max(0, job.attempts - 1), len(delays) - 1)
        return delays[index]
    return max(0, config.job_retry_delay_seconds) * (2 ** max(0, job.attempts - 1))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    serve(args.config)


if __name__ == "__main__":
    main()
