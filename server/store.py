from __future__ import annotations

import sqlite3
from pathlib import Path


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
