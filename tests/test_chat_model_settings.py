"""Deterministic settings persistence, expiry, and competing-operator checks."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from chat_model_settings import (
    ChatModelSettingsStore,
    DEFAULT_DURATION_SECONDS,
    InvalidModelSelection,
    MAX_DURATION_SECONDS,
    MODEL_CATALOG,
    StaleSettingsRevision,
    get_model_capability,
    parse_duration,
    parse_model_selection,
    validate_selection,
)


class ModelSelectionTests(unittest.TestCase):
    def test_catalog_and_reasoning_are_explicit(self):
        self.assertEqual(["astra", "sol", "terra", "luna", "spark"], [entry.aliases[0] for entry in MODEL_CATALOG])
        self.assertTrue(all(entry.supports_images for entry in MODEL_CATALOG[:-1]))
        self.assertFalse(MODEL_CATALOG[-1].supports_images)
        self.assertEqual(parse_model_selection("spark 15m"), parse_model_selection("qwen-auto 15m"))
        self.assertEqual(("spark-qwen", "auto"), validate_selection("qwen", "auto"))
        self.assertEqual(("gpt-6-astra", "max"), validate_selection("ASTRA", "MAX"))
        self.assertEqual(("gpt-6.1-terra", "none"), validate_selection("terra", "none"))
        self.assertEqual(("gpt-6-luna", "none"), validate_selection("luna", "none"))
        self.assertEqual("gpt-6.1-sol", get_model_capability("sol").model)
        for model, effort in (("astra", "none"), ("sol", "none"), ("terra", "ultra"),
                              ("unknown", "low"), ("gpt-6-astra-snapshot", "low")):
            with self.subTest(model=model, effort=effort), self.assertRaises(InvalidModelSelection):
                validate_selection(model, effort)

    def test_cli_alias_canonical_and_explicit_duration(self):
        result = parse_model_selection("astra-low 15m")
        self.assertEqual(("gpt-6-astra", "low", 900),
                         (result.model, result.reasoning_effort, result.duration_seconds))
        result = parse_model_selection(["gpt-6.1-terra", "none", "1h"])
        self.assertEqual(("gpt-6.1-terra", "none", 3600),
                         (result.model, result.reasoning_effort, result.duration_seconds))
        self.assertEqual(DEFAULT_DURATION_SECONDS, parse_model_selection("luna").duration_seconds)
        self.assertEqual("low", parse_model_selection("sol 7d").reasoning_effort)
        self.assertIsNone(parse_model_selection("astra-max persistent").duration_seconds)
        self.assertIsNone(parse_model_selection("terra none until-reset").duration_seconds)

    def test_cli_rejects_unknown_unbounded_or_ambiguous_inputs(self):
        for value in ("", "astra low 1m extra", "astra ultra", "astra-none", "0m", "astra 8d",
                      "astra 0m", "astra 15s", "astra 1.5h", "astra off", "astra -1m",
                      "astra 99999999999999999999999999999999999999d", "unknown low"):
            with self.subTest(value=value), self.assertRaises(InvalidModelSelection):
                parse_model_selection(value)
        self.assertEqual(60, parse_duration("1m"))
        self.assertEqual(MAX_DURATION_SECONDS, parse_duration("7d"))


class ChatSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "state.sqlite3"
        self.now = 1000.0
        self.store = ChatModelSettingsStore(self.path, clock=lambda: self.now)

    def resolve(self, chat=-1001, **kwargs):
        return self.store.resolve(chat, "legacy-current-default", "low", **kwargs)

    def test_defaults_do_not_require_registry_and_do_not_create_chat_rows(self):
        profile = self.resolve()
        self.assertEqual(("legacy-current-default", "low", False, None, 0),
                         (profile.model, profile.reasoning_effort, profile.overridden,
                          profile.expires_at, profile.revision))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM chat_model_settings").fetchone()[0])

    def test_request_snapshot_is_immutable_and_chat_isolated(self):
        first = self.store.apply(-1001, "astra", "high", 10, expected_revision=0)
        self.assertEqual(("gpt-6-astra", "high", True, 1010, 1),
                         (first.model, first.reasoning_effort, first.overridden, first.expires_at, first.revision))
        with self.assertRaises(FrozenInstanceError):
            first.model = "changed"
        self.store.apply(-1001, "luna", "none", 20, expected_revision=1)
        self.assertEqual("gpt-6-astra", first.model)
        self.assertEqual("gpt-6-luna", self.resolve().model)
        self.assertFalse(self.resolve(-1002).overridden)

    def test_restart_expiry_uses_current_default_and_persists_revision_once(self):
        self.store.apply(-1001, "astra", "high", 10)
        self.now = 1010
        restarted = ChatModelSettingsStore(self.path, clock=lambda: self.now)
        profile = restarted.resolve(-1001, "new-default", "medium")
        self.assertEqual(("new-default", "medium", False, 2),
                         (profile.model, profile.reasoning_effort, profile.overridden, profile.revision))
        self.assertEqual(2, self.resolve().revision)
        with self.assertRaises(StaleSettingsRevision):
            restarted.apply(-1001, "astra", "low", 10, expected_revision=1)
        self.assertFalse(self.resolve().overridden)

    def test_expiry_and_stale_apply_are_atomic_even_without_prior_resolve(self):
        self.store.apply(-1001, "astra", "low", 5)
        with self.assertRaises(StaleSettingsRevision):
            self.store.apply(-1001, "luna", "low", 10, expected_revision=1, now=1005)
        self.assertEqual(2, self.resolve(now=1005).revision)
        changed = self.store.apply(-1001, "luna", "low", 10, expected_revision=2, now=1005)
        self.assertEqual(3, changed.revision)

    def test_new_selection_cannot_be_undone_by_old_expiry_or_reset(self):
        self.store.apply(-1001, "astra", "low", 5)
        latest = self.store.apply(-1001, "terra", "medium", 100, expected_revision=1, now=1004)
        self.assertEqual(2, latest.revision)
        with self.assertRaises(StaleSettingsRevision):
            self.store.reset(-1001, expected_revision=1, now=1006)
        profile = self.resolve(now=1006)
        self.assertEqual("gpt-6.1-terra", profile.model)
        self.assertEqual(2, profile.revision)

    def test_reset_tombstone_invalidates_prior_panels_after_restart(self):
        self.store.apply(-1001, "sol", "low")
        self.assertEqual(2, self.store.reset(-1001, expected_revision=1))
        restarted = ChatModelSettingsStore(self.path)
        profile = restarted.resolve(-1001, "new-current-default", "high")
        self.assertFalse(profile.overridden)
        self.assertEqual("new-current-default", profile.model)
        self.assertEqual(2, profile.revision)
        with self.assertRaises(StaleSettingsRevision):
            restarted.apply(-1001, "sol", "low", expected_revision=1)
        self.assertEqual(3, restarted.reset(-1001, expected_revision=2))

    def test_persistent_selection_has_no_timer(self):
        self.store.apply(-1001, "terra", "none", None)
        profile = self.resolve(now=1000 + 100 * 86400)
        self.assertTrue(profile.overridden)
        self.assertEqual("none", profile.reasoning_effort)
        self.assertIsNone(profile.expires_at)

    def test_invalid_request_never_changes_selection(self):
        self.store.apply(-1001, "astra", "low", 60)
        for changes in ({"model": "unknown"}, {"effort": "none"}, {"duration_seconds": 0},
                        {"duration_seconds": True}, {"duration_seconds": 604801},
                        {"expected_revision": -1}, {"expected_revision": True}, {"now": float("inf")}):
            arguments = dict(chat_id=-1001, model="astra", effort="low", duration_seconds=60)
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(InvalidModelSelection):
                self.store.apply(**arguments)
        self.assertEqual(1, self.resolve().revision)
        for scope in (0, True, "-1001", 2**64):
            with self.subTest(scope=scope), self.assertRaises(InvalidModelSelection):
                self.resolve(scope)

    def test_malformed_persisted_selection_falls_back_without_provider_authority(self):
        for index, changes in enumerate((
            ("unknown-model", "low", None), ("gpt-6-astra", "none", None),
            ("gpt-6-astra", "low", "bad-time"), ("gpt-6-astra", "low", float("inf")),
            (None, "low", None), ("astra", "low", None),
        )):
            chat = -1001 - index
            self.store.apply(chat, "astra", "low", 60)
            with sqlite3.connect(self.path) as conn:
                conn.execute("UPDATE chat_model_settings SET model=?, reasoning_effort=?, expires_at=? WHERE chat_id=?",
                             (*changes, chat))
            profile = self.resolve(chat)
            self.assertFalse(profile.overridden)
            self.assertEqual("legacy-current-default", profile.model)
            self.assertEqual(2, profile.revision)
            self.assertEqual(2, self.resolve(chat).revision)

    def test_competing_instances_cannot_both_apply_same_revision(self):
        self.store.apply(-1001, "astra", "low", 60)
        barrier = threading.Barrier(2)
        def change(model):
            contender = ChatModelSettingsStore(self.path, clock=lambda: self.now)
            barrier.wait()
            try:
                return contender.apply(-1001, model, "high", 60, expected_revision=1).model
            except StaleSettingsRevision:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(change, ("terra", "luna")))
        self.assertEqual(1, outcomes.count("stale"))
        self.assertIn(self.resolve().model, outcomes)
        self.assertEqual(2, self.resolve().revision)

    def test_concurrent_expiry_resolves_increment_revision_only_once(self):
        self.store.apply(-1001, "astra", "low", 5)
        barrier = threading.Barrier(4)
        def read(_):
            contender = ChatModelSettingsStore(self.path)
            barrier.wait()
            return contender.resolve(-1001, "new-default", "medium", now=1005)
        with ThreadPoolExecutor(max_workers=4) as pool:
            profiles = list(pool.map(read, range(4)))
        self.assertEqual({2}, {profile.revision for profile in profiles})
        self.assertTrue(all(not profile.overridden for profile in profiles))
        selected = self.store.apply(-1001, "luna", "low", 10, expected_revision=2, now=1005)
        self.assertEqual(3, selected.revision)

    def test_corrupt_revision_cannot_reuse_an_old_panel_revision(self):
        for index, revision in enumerate((0, -1, "corrupt", 1.5)):
            chat = -2001 - index
            self.store.apply(chat, "astra", "low", 60)
            with sqlite3.connect(self.path) as conn:
                conn.execute("UPDATE chat_model_settings SET revision=? WHERE chat_id=?", (revision, chat))
            profile = self.resolve(chat)
            self.assertFalse(profile.overridden)
            self.assertEqual("legacy-current-default", profile.model)
            for operation in (
                lambda: self.store.apply(chat, "terra", "low", 60, expected_revision=0),
                lambda: self.store.apply(chat, "terra", "low", 60, expected_revision=1),
                lambda: self.store.reset(chat),
            ):
                with self.assertRaisesRegex(InvalidModelSelection, "malformed_settings_revision"):
                    operation()
            with sqlite3.connect(self.path) as conn:
                self.assertEqual(revision, conn.execute(
                    "SELECT revision FROM chat_model_settings WHERE chat_id=?", (chat,),
                ).fetchone()[0])

    def test_existing_database_tables_and_rows_are_preserved(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE unrelated_records (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO unrelated_records VALUES (1, 'synthetic retained record')")
        other = ChatModelSettingsStore(self.path)
        other.apply(-1001, "astra", "low", 60)
        other.reset(-1001)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual([(1, "synthetic retained record")],
                             conn.execute("SELECT * FROM unrelated_records").fetchall())
            self.assertEqual("ok", conn.execute("PRAGMA quick_check").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
