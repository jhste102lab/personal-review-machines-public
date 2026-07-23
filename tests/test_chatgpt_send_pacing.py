from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from server.app import ChatGPTSendGate, _retry_delay_seconds
from server.config import Config, load_config
from server.review_runner import ReviewOutcome, run_review
from server.store import ReviewJob, ReviewStore


def make_config(**overrides: object) -> Config:
    values: dict[str, object] = {
        "webhook_secret": "test-secret",
        "allowed_repositories": frozenset({"owner/repo"}),
        "allowed_author_associations": frozenset({"OWNER"}),
        "repository_author_associations": {},
        "work_dir": Path("/tmp/review-work"),
        "db_path": Path("/tmp/reviews.sqlite3"),
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


class MutableClock:
    def __init__(self, now: float) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RecordingSendGate:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @contextmanager
    def wait_for_turn(self):
        self.events.append("gate_enter")
        try:
            yield
        finally:
            self.events.append("gate_exit")

    def defer_after_success(self) -> float:
        self.events.append("session_created")
        return 60.0

    def defer_after_pre_send_failure(self, delay_seconds: float) -> None:
        self.events.append(f"pre_send_failure:{delay_seconds}")

    def defer_after_delivery_uncertain(self) -> float:
        self.events.append("delivery_uncertain")
        return 60.0


class ChatGPTSendPacingTests(unittest.TestCase):
    def test_successful_send_persists_global_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "reviews.sqlite3")
            clock = MutableClock(100.0)
            gate = ChatGPTSendGate(
                store,
                make_config(chatgpt_success_gap_min_seconds=45, chatgpt_success_gap_max_seconds=75),
                clock=clock,
                sleep=clock.sleep,
                random_interval=lambda minimum, maximum: 60.0,
            )

            with gate.wait_for_turn():
                self.assertEqual(gate.defer_after_success(), 60.0)

            self.assertEqual(store.chatgpt_next_send_at(), 160.0)
            with gate.wait_for_turn():
                pass
            self.assertEqual(clock.sleeps, [60.0])

    def test_pre_send_cooldown_never_moves_backward(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewStore(Path(temp_dir) / "reviews.sqlite3")
            store.defer_chatgpt_send_until(300.0)
            store.defer_chatgpt_send_until(200.0)
            self.assertEqual(store.chatgpt_next_send_at(), 300.0)

    def test_chatgpt_pre_send_retry_schedule_uses_configured_delays(self) -> None:
        config = make_config(chatgpt_pre_send_retry_delays_seconds=(90, 150, 300))
        outcome = ReviewOutcome(False, retryable=True, reason="chatgpt_prompt_send_failed")
        for attempts, expected in ((1, 90), (2, 150), (3, 300), (4, 300)):
            job = ReviewJob("owner/repo", 1, "chatgpt_high", "review", {}, attempts)
            self.assertEqual(_retry_delay_seconds(config, job, outcome), expected)

    def test_config_rejects_reversed_success_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "webhook_secret": "test-secret",
                        "allowed_repositories": ["owner/repo"],
                        "chatgpt_success_gap_min_seconds": 75,
                        "chatgpt_success_gap_max_seconds": 45,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "chatgpt_success_gap_max_seconds"):
                load_config(path)

    def test_checkout_completes_before_chatgpt_send_lane_is_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events: list[str] = []
            config = make_config(work_dir=Path(temp_dir) / "work")
            event = {
                "repository": {"full_name": "owner/repo"},
                "issue": {"number": 1},
                "comment": {"id": 2},
            }

            def checkout(_: str, __: int, checkout_dir: Path, *, reuse_existing: bool = False) -> None:
                events.append("checkout")
                checkout_dir.mkdir(parents=True)

            def run_agent(**_: object) -> int:
                events.append("agent")
                return 0

            with (
                patch("server.review_runner._checkout_pr", side_effect=checkout),
                patch("server.review_runner._run_text", return_value="deadbeef\n"),
                patch("server.review_runner._run_agent_with_watchdog", side_effect=run_agent),
            ):
                outcome = run_review(
                    config,
                    event,
                    "chatgpt_high",
                    "review",
                    chatgpt_send_gate=RecordingSendGate(events),  # type: ignore[arg-type]
                    chatgpt_pre_send_retry_delay_seconds=90,
                )

            self.assertTrue(outcome.success)
            self.assertEqual(events, ["checkout", "gate_enter", "agent", "session_created", "gate_exit"])


if __name__ == "__main__":
    unittest.main()
