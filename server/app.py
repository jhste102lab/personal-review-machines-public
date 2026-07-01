from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config, load_config
from .review_runner import parse_request, run_review
from .store import ReviewStore


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
        if not self.store.claim_comment(repo, comment_id, engine):
            return {"ignored": "already_processed", "comment_id": comment_id}

        thread = threading.Thread(
            target=run_review,
            args=(self.config, event, engine, instruction),
            name=f"review-{repo}-{comment_id}",
            daemon=True,
        )
        thread.start()
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

    WebhookHandler.config = config
    WebhookHandler.store = store
    server = ThreadingHTTPServer((config.bind_host, config.bind_port), WebhookHandler)
    LOG.info("listening on %s:%s", config.bind_host, config.bind_port)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    serve(args.config)


if __name__ == "__main__":
    main()
