from __future__ import annotations

import json
from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from context_selection_replay import (
    ContextSelectionReplayError,
    REVIEW_MANIFEST_FILENAME,
    REVIEW_POOL_FILENAME,
    _write_exclusive,
    build_private_review_pool,
    collect_review_packets,
)


PRIVATE_SENTINEL = "PRIVATE_REPLAY_SENTINEL"


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            chat_type TEXT,
            created_at TEXT NOT NULL,
            sender_label TEXT,
            user_id INTEGER,
            username TEXT,
            is_bot INTEGER NOT NULL DEFAULT 0,
            text TEXT,
            content_kind TEXT,
            attachment_type TEXT,
            telegram_file_id TEXT,
            telegram_unique_id TEXT,
            local_media_path TEXT,
            mime_type TEXT,
            vision_summary TEXT,
            source_url TEXT,
            source_title TEXT,
            reply_to_message_id INTEGER,
            forward_origin TEXT,
            raw_note TEXT,
            source_text TEXT
        );
        CREATE TABLE provenance_runs (
            run_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            trigger_message_id INTEGER,
            input_memory_id INTEGER,
            route TEXT,
            status TEXT,
            started_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE provenance_outputs (
            run_id TEXT,
            memory_id INTEGER,
            ordinal INTEGER,
            part_count INTEGER
        );
        CREATE TABLE system_events (
            id INTEGER PRIMARY KEY,
            created_at TEXT,
            level TEXT,
            component TEXT,
            event_type TEXT,
            chat_id INTEGER,
            user_id INTEGER,
            route TEXT,
            duration_ms INTEGER,
            message TEXT,
            details_json TEXT
        );
        """
    )
    rows = [
        (1, 101, 11, "2026-01-01T11:58:00+00:00", "alpha", 501, 0, "Earlier evidence", None),
        (2, 101, 12, "2026-01-01T11:59:00+00:00", "Aigan", None, 1, "Prior bot answer", 11),
        (3, 101, 13, "2026-01-01T12:00:00+00:00", "alpha", 501, 0, f"/ai Why? {PRIVATE_SENTINEL}", 12),
        (4, 101, 14, "2026-01-01T13:00:00+00:00", "beta", 502, 0, "What is current now?", None),
        (5, 101, 15, "2026-01-01T12:01:00+00:00", "Aigan", None, 1, "Future delivered answer", 13),
    ]
    connection.executemany(
        """
        INSERT INTO messages (
            id, chat_id, message_id, chat_type, created_at, sender_label,
            user_id, is_bot, text, content_kind, reply_to_message_id
        ) VALUES (?, ?, ?, 'private', ?, ?, ?, ?, ?, 'text', ?)
        """,
        rows,
    )
    connection.execute(
        """
        INSERT INTO provenance_runs (
            run_id, chat_id, trigger_message_id, input_memory_id, route,
            status, started_at, completed_at
        ) VALUES ('run-private', 101, 13, 3, 'normal', 'delivered',
                  '2026-01-01T12:00:02+00:00', '2026-01-01T12:01:00+00:00')
        """
    )
    connection.execute(
        "INSERT INTO provenance_outputs (run_id, memory_id, ordinal, part_count) VALUES ('run-private', 5, 0, 1)"
    )
    connection.executemany(
        """
        INSERT INTO system_events (
            id, created_at, level, component, event_type, chat_id, user_id, route
        ) VALUES (?, ?, 'info', 'routing', 'route_decision', 101, ?, ?)
        """,
        [
            (1, "2026-01-01T12:00:02+00:00", 501, "normal"),
            (2, "2026-01-01T13:00:02+00:00", 502, "memory_recall"),
        ],
    )
    connection.commit()
    connection.close()


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_all_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_all_keys(child))
        return keys
    return set()


class ContextSelectionReplayTests(unittest.TestCase):
    def test_collection_uses_opaque_links_and_aggregate_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            packets, summary = collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            connection.close()

        self.assertEqual(3, len(packets))
        self.assertEqual(1, summary["correlation_counts"]["exact_provenance"])
        self.assertEqual(1, summary["correlation_counts"]["approximate_route_event"])
        self.assertEqual(1, summary["correlation_counts"]["outbound_reply_link"])
        self.assertEqual(0, summary["correlation_counts"]["approximate_bot_output"])
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(summary, ensure_ascii=False))
        forbidden_keys = {
            "id",
            "chat_id",
            "message_id",
            "user_id",
            "username",
            "source_url",
            "local_media_path",
            "telegram_file_id",
            "telegram_unique_id",
            "run_id",
            "raw_json",
        }
        self.assertFalse(_all_keys(packets) & forbidden_keys)
        self.assertIn(PRIVATE_SENTINEL, json.dumps(packets, ensure_ascii=False))
        self.assertTrue(all(packet["packet_key"].startswith("packet-") for packet in packets))
        self.assertTrue(all(packet["case_key"].startswith("case-") for packet in packets))
        self.assertTrue(all(packet["cluster_key"].startswith("cluster-") for packet in packets))
        self.assertTrue(all(packet["cluster_method"] == "inactivity_session" for packet in packets))
        self.assertTrue(all(packet["cluster_inactivity_minutes"] == 30 for packet in packets))
        exact = next(packet for packet in packets if packet["correlation_kind"] == "exact_provenance")
        self.assertIn("explicit_reply", exact["suggested_classes"])
        self.assertEqual([], exact["target"]["provenance_output_source_keys"])
        self.assertTrue(exact["run_key"].startswith("run-"))
        self.assertEqual("delivered", exact["run_status"])
        self.assertEqual(1, len(exact["run_output_source_keys"]))

    def test_multiple_exact_runs_are_preserved_but_share_one_case_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            connection.execute(
                """
                INSERT INTO provenance_runs (
                    run_id, chat_id, trigger_message_id, input_memory_id, route,
                    status, started_at, completed_at
                ) VALUES ('run-private-retry', 101, 13, 3, 'normal', 'failed',
                          '2026-01-01T12:00:03+00:00', '2026-01-01T12:00:04+00:00')
                """
            )
            connection.commit()
            packets, summary = collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            connection.close()

        exact = [packet for packet in packets if packet["correlation_kind"] == "exact_provenance"]
        self.assertEqual(2, len(exact))
        self.assertEqual(1, len({packet["case_key"] for packet in exact}))
        self.assertEqual(2, len({packet["packet_key"] for packet in exact}))
        self.assertEqual(2, len({packet["run_key"] for packet in exact}))
        self.assertEqual(1, summary["correlation_counts"]["repeated_exact_targets"])

    def test_message_history_is_loaded_once_instead_of_per_packet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            connection.close()

        normalized = [" ".join(statement.split()).casefold() for statement in statements]
        self.assertEqual(1, normalized.count("select * from messages"))
        self.assertFalse(
            any(
                "from messages where chat_id" in statement
                for statement in normalized
            )
        )

    def test_manifest_route_counts_never_copy_untrusted_route_values(self) -> None:
        private_route = "PRIVATE_ROUTE_SENTINEL"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            connection.execute("UPDATE provenance_runs SET route = ?", (private_route,))
            connection.commit()
            packets, summary = collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            connection.close()
        self.assertIn(private_route, json.dumps(packets, ensure_ascii=False))
        self.assertNotIn(private_route, json.dumps(summary, ensure_ascii=False))
        self.assertEqual(1, summary["route_counts"]["other"])

    def test_route_event_matches_same_user_and_speaker_keys_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            connection.executemany(
                """
                INSERT INTO messages (
                    id, chat_id, message_id, chat_type, created_at, sender_label,
                    user_id, username, is_bot, text, content_kind
                ) VALUES (?, 101, ?, 'private', ?, NULL, NULL, ?, 0, ?, 'text')
                """,
                [
                    (8, 18, "2026-01-01T13:00:01+00:00", "502", "wrong nearby speaker"),
                    (9, 19, "2026-01-01T11:57:00+00:00", "501", "typed username source"),
                ],
            )
            connection.commit()
            packets, _summary = collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            connection.close()

        route_packet = next(
            packet for packet in packets if packet["correlation_kind"] == "approximate_route_event"
        )
        self.assertEqual("What is current now?", route_packet["target"]["text"])
        self.assertEqual("user_id", route_packet["target"]["speaker_identity_kind"])
        username_source = next(
            source for source in route_packet["sources"] if source["text"] == "typed username source"
        )
        self.assertEqual("username", username_source["speaker_identity_kind"])
        self.assertNotEqual(route_packet["target"]["speaker_key"], username_source["speaker_key"])

    def test_cli_bounds_sqlite_failures_without_payload_or_path(self) -> None:
        from scripts import build_context_selection_replay as command

        private_sentinel = "PRIVATE_SQLITE_SENTINEL"
        stderr = StringIO()
        with patch.object(command, "_private_root", return_value=Path("ignored")):
            with patch.object(
                command,
                "build_private_review_pool",
                side_effect=sqlite3.OperationalError(private_sentinel),
            ):
                with patch("sys.argv", ["build_context_selection_replay.py"]):
                    with redirect_stderr(stderr):
                        result = command.main()
        self.assertEqual(2, result)
        self.assertEqual(
            "context-selection replay build failed: OperationalError\n",
            stderr.getvalue(),
        )
        self.assertNotIn(private_sentinel, stderr.getvalue())

    def test_private_root_uses_ignored_repo_data_when_data_is_absent(self) -> None:
        from scripts import build_context_selection_replay as command

        with tempfile.TemporaryDirectory() as directory:
            repo_root = Path(directory)
            (repo_root / ".gitignore").write_text("data/\n", encoding="utf-8")
            with patch.object(command.shutil, "which", return_value=None):
                private_root = command._private_root(repo_root)
            self.assertEqual(
                (repo_root / "data/research/context-selection-v1").resolve(),
                private_root,
            )
            self.assertFalse((repo_root / "data").exists())

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available")
    def test_private_root_rejects_dangling_data_symlink(self) -> None:
        from scripts import build_context_selection_replay as command

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = base / "repo"
            repo_root.mkdir()
            (repo_root / ".gitignore").write_text("data/\n", encoding="utf-8")
            (repo_root / "data").symlink_to(base / "missing", target_is_directory=True)
            xdg_data_home = base / "xdg"
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data_home)}):
                private_root = command._private_root(repo_root)
            self.assertEqual(
                (xdg_data_home / "aigan-context-selection-v1").resolve(),
                private_root,
            )
            with self.assertRaises(ValueError):
                private_root.relative_to(repo_root.resolve())

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available")
    def test_private_root_rejects_existing_intermediate_symlink(self) -> None:
        from scripts import build_context_selection_replay as command

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = base / "repo"
            (repo_root / "data").mkdir(parents=True)
            (repo_root / ".gitignore").write_text("data/\n", encoding="utf-8")
            external_root = base / "external"
            external_root.mkdir()
            (repo_root / "data/research").symlink_to(
                external_root,
                target_is_directory=True,
            )
            xdg_data_home = base / "xdg"
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data_home)}):
                private_root = command._private_root(repo_root)
            self.assertEqual(
                (xdg_data_home / "aigan-context-selection-v1").resolve(),
                private_root,
            )
            with self.assertRaises(ValueError):
                private_root.relative_to(repo_root.resolve())

    def test_cli_bounds_corrupt_row_failures_without_payload(self) -> None:
        from scripts import build_context_selection_replay as command

        private_sentinel = "PRIVATE_CORRUPT_ROW_SENTINEL"
        stderr = StringIO()
        with patch.object(command, "_private_root", return_value=Path("ignored")):
            with patch.object(
                command,
                "build_private_review_pool",
                side_effect=ValueError(private_sentinel),
            ):
                with patch("sys.argv", ["build_context_selection_replay.py"]):
                    with redirect_stderr(stderr):
                        result = command.main()
        self.assertEqual(2, result)
        self.assertEqual(
            "context-selection replay build failed: ValueError\n",
            stderr.getvalue(),
        )
        self.assertNotIn(private_sentinel, stderr.getvalue())

    def test_cli_surfaces_bounded_replay_contract_diagnostic(self) -> None:
        from scripts import build_context_selection_replay as command

        stderr = StringIO()
        with patch.object(command, "_private_root", return_value=Path("ignored")):
            with patch.object(
                command,
                "build_private_review_pool",
                side_effect=ContextSelectionReplayError("private output already exists"),
            ):
                with patch("sys.argv", ["build_context_selection_replay.py"]):
                    with redirect_stderr(stderr):
                        result = command.main()
        self.assertEqual(2, result)
        self.assertEqual(
            "context-selection replay build failed: private output already exists\n",
            stderr.getvalue(),
        )

    def test_reply_chain_never_admits_a_future_duplicate(self) -> None:
        future_sentinel = "FUTURE_REPLY_SENTINEL"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            connection.executemany(
                """
                INSERT INTO messages (
                    id, chat_id, message_id, chat_type, created_at, sender_label,
                    user_id, is_bot, text, content_kind
                ) VALUES (?, 101, ?, 'private', ?, ?, ?, 0, ?, 'text')
                """,
                [
                    (
                        10,
                        12,
                        "2026-01-01T14:00:00+00:00",
                        "future",
                        999,
                        future_sentinel,
                    ),
                    (
                        11,
                        17,
                        "2026-01-01T11:59:30+00:00",
                        "distractor",
                        998,
                        "Recent distractor",
                    ),
                ],
            )
            connection.commit()
            packets, _summary = collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=1,
                reply_depth=8,
            )
            connection.close()
        exact = next(packet for packet in packets if packet["correlation_kind"] == "exact_provenance")
        source_texts = {source["text"] for source in exact["sources"]}
        self.assertIn("Recent distractor", source_texts)
        self.assertIn("Prior bot answer", source_texts)
        self.assertNotIn(future_sentinel, json.dumps(packets, ensure_ascii=False))

    def test_cluster_session_does_not_split_at_wall_clock_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            _create_database(database)
            connection = sqlite3.connect(database)
            connection.executemany(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                [
                    ("2026-01-01T12:29:00+00:00", 1),
                    ("2026-01-01T12:30:00+00:00", 2),
                    ("2026-01-01T12:31:00+00:00", 3),
                ],
            )
            connection.execute(
                "UPDATE provenance_runs SET started_at = '2026-01-01T12:31:02+00:00'"
            )
            connection.execute(
                "UPDATE system_events SET created_at = '2026-01-01T12:31:02+00:00' WHERE id = 1"
            )
            connection.commit()
            packets, _summary = collect_review_packets(
                connection,
                hmac_key=b"k" * 32,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            connection.close()
        exact = next(packet for packet in packets if packet["correlation_kind"] == "exact_provenance")
        reply = next(packet for packet in packets if packet["correlation_kind"] == "outbound_reply_link")
        self.assertEqual(exact["cluster_key"], reply["cluster_key"])

    @unittest.skipIf(os.name == "nt", "private replay writer requires POSIX owner-only modes")
    def test_private_writer_is_exclusive_and_manifest_is_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database = base / "replay.sqlite3"
            private_root = base / "private"
            _create_database(database)
            manifest = build_private_review_pool(
                database_path=database,
                private_root=private_root,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
            self.assertTrue((private_root / REVIEW_POOL_FILENAME).is_file())
            self.assertTrue((private_root / REVIEW_MANIFEST_FILENAME).is_file())
            self.assertTrue(manifest["contains_private_payloads"])
            self.assertFalse(manifest["github_publishable"])
            self.assertFalse(manifest["labels_complete"])
            self.assertNotIn(PRIVATE_SENTINEL, json.dumps(manifest, ensure_ascii=False))
            with self.assertRaises(ContextSelectionReplayError):
                build_private_review_pool(
                    database_path=database,
                    private_root=private_root,
                    lookback_days=30,
                    recent_limit=20,
                    reply_depth=8,
                )

    @unittest.skipIf(os.name == "nt", "private replay writer requires POSIX owner-only modes")
    def test_read_only_database_uri_encodes_reserved_path_characters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            special = Path(directory) / "uri ? fragment #"
            special.mkdir()
            database = special / "replay.sqlite3"
            private_root = special / "private"
            _create_database(database)
            manifest = build_private_review_pool(
                database_path=database,
                private_root=private_root,
                lookback_days=30,
                recent_limit=20,
                reply_depth=8,
            )
        self.assertEqual(3, manifest["packet_count"])

    @unittest.skipIf(os.name == "nt", "private replay writer requires POSIX owner-only modes")
    def test_exclusive_publish_never_replaces_racing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "private-output.json"
            real_link = os.link

            def racing_link(source: str | bytes | os.PathLike[str], target: str | bytes | os.PathLike[str]) -> None:
                Path(target).write_bytes(b"winner")
                os.chmod(target, 0o600)
                real_link(source, target)

            with patch("context_selection_replay.os.link", side_effect=racing_link):
                with self.assertRaises(ContextSelectionReplayError):
                    _write_exclusive(destination, b"loser")

            self.assertEqual(b"winner", destination.read_bytes())
            self.assertEqual([], list(root.glob(".*.tmp-*")))

    @unittest.skipUnless(os.name == "nt", "Windows-specific fail-closed contract")
    def test_private_writer_fails_closed_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database = base / "replay.sqlite3"
            _create_database(database)
            with self.assertRaises(ContextSelectionReplayError):
                build_private_review_pool(
                    database_path=database,
                    private_root=base / "private",
                )


if __name__ == "__main__":
    unittest.main()
