from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewJob:
    repository: str
    comment_id: int
    engine: str
    instruction: str
    event: dict
    attempts: int


class ReviewStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def _init(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_comments (
                    repository TEXT NOT NULL,
                    comment_id INTEGER NOT NULL,
                    engine TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (repository, comment_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                    repository TEXT NOT NULL,
                    comment_id INTEGER NOT NULL,
                    engine TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (repository, comment_id)
                )
                """
            )

    def claim_comment(self, repository: str, comment_id: int, engine: str) -> bool:
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO processed_comments(repository, comment_id, engine) VALUES (?, ?, ?)",
                    (repository.lower(), comment_id, engine),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def enqueue_job(self, repository: str, comment_id: int, engine: str, instruction: str, event: dict) -> bool:
        repo = repository.lower()
        event_json = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO processed_comments(repository, comment_id, engine) VALUES (?, ?, ?)",
                    (repo, comment_id, engine),
                )
                db.execute(
                    """
                    INSERT INTO review_jobs(
                        repository,
                        comment_id,
                        engine,
                        instruction,
                        event_json,
                        status,
                        next_run_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (repo, comment_id, engine, instruction, event_json, time.time()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def recover_interrupted_jobs(self, non_retryable_engines: set[str] | frozenset[str]) -> tuple[int, int]:
        requeued = 0
        failed = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT repository, comment_id, engine FROM review_jobs WHERE status = 'running'"
            ).fetchall()
            for repository, comment_id, engine in rows:
                if str(engine) in non_retryable_engines:
                    db.execute(
                        """
                        UPDATE review_jobs
                        SET status = 'failed',
                            updated_at = CURRENT_TIMESTAMP,
                            last_error = 'service restarted after ChatGPT dispatch may have occurred; not retried'
                        WHERE repository = ? AND comment_id = ?
                        """,
                        (repository, int(comment_id)),
                    )
                    failed += 1
                else:
                    db.execute(
                        """
                        UPDATE review_jobs
                        SET status = 'queued',
                            next_run_at = ?,
                            updated_at = CURRENT_TIMESTAMP,
                            last_error = 'service restarted while job was running'
                        WHERE repository = ? AND comment_id = ?
                        """,
                        (time.time(), repository, int(comment_id)),
                    )
                    requeued += 1
        return requeued, failed

    def list_queued_jobs(self) -> list[tuple[ReviewJob, float]]:
        now = time.time()
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT repository, comment_id, engine, instruction, event_json, attempts, next_run_at
                FROM review_jobs
                WHERE status = 'queued'
                ORDER BY next_run_at ASC, created_at ASC
                """,
            ).fetchall()
        return [(self._row_to_job(row[:6]), max(0.0, float(row[6]) - now)) for row in rows]

    def start_job(self, repository: str, comment_id: int) -> ReviewJob | None:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT repository, comment_id, engine, instruction, event_json, attempts
                FROM review_jobs
                WHERE repository = ?
                  AND comment_id = ?
                  AND status = 'queued'
                  AND next_run_at <= ?
                """,
                (repository.lower(), int(comment_id), now),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row[5]) + 1
            db.execute(
                """
                UPDATE review_jobs
                SET status = 'running', attempts = ?, updated_at = CURRENT_TIMESTAMP
                WHERE repository = ? AND comment_id = ? AND status = 'queued'
                """,
                (attempts, repository.lower(), int(comment_id)),
            )
        return self._row_to_job((*row[:5], attempts))

    @staticmethod
    def _row_to_job(row: tuple[object, ...]) -> ReviewJob:
        repository, comment_id, engine, instruction, event_json, attempts = row
        return ReviewJob(
            repository=str(repository),
            comment_id=int(comment_id),
            engine=str(engine),
            instruction=str(instruction),
            event=json.loads(str(event_json)),
            attempts=int(attempts),
        )

    def finish_job(self, repository: str, comment_id: int) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE review_jobs
                SET status = 'done',
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = ''
                WHERE repository = ?
                  AND comment_id = ?
                """,
                (repository.lower(), comment_id),
            )

    def retry_job(self, repository: str, comment_id: int, delay_seconds: int, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE review_jobs
                SET status = 'queued',
                    next_run_at = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = ?
                WHERE repository = ?
                  AND comment_id = ?
                """,
                (time.time() + delay_seconds, error[:1000], repository.lower(), comment_id),
            )

    def fail_job(self, repository: str, comment_id: int, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE review_jobs
                SET status = 'failed',
                    updated_at = CURRENT_TIMESTAMP,
                    last_error = ?
                WHERE repository = ?
                  AND comment_id = ?
                """,
                (error[:1000], repository.lower(), comment_id),
            )
