import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory import MemoryStore


class MemoryRetentionTests(unittest.TestCase):
    def test_selected_chat_keeps_old_messages_and_multipart_deliveries(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = self.enterContext(closing(MemoryStore(root / "memory.sqlite3", full_history_chat_ids={11})))
        shared = root / "shared.jpg"
        exclusive = root / "expired.jpg"
        for path in (shared, exclusive):
            path.write_bytes(b"fixture")
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=365)
        rows = {}
        originals = {}
        media = {(11, 0): shared, (22, 0): shared, (22, 1): exclusive}
        for chat, run in ((11, "a" * 32), (22, "b" * 32)):
            original = store.save_message(chat_id=chat, message_id=4, text="searchable history", created_at=old)
            originals[chat] = original
            store.upsert_embedding(message_id=original, chat_id=chat, model="test", dimensions=1,
                                   content_hash="test", embedding=[1.0])
            for part, timestamp in enumerate((old, now)):
                path = media.get((chat, part))
                row = store.save_message(
                    chat_id=chat, message_id=part + 1, is_bot=True, text="delivery",
                    created_at=timestamp, local_media_path=str(path) if path else "",
                )
                rows[chat, part] = row
                store.record_provenance_output(
                    run_id=run, chat_id=chat, trigger_message_id=None, input_memory_id=None,
                    route="retention", started_at=old, output_memory_id=row,
                    output_ordinal=part, output_part_count=2,
                )
        recent = store.save_message(chat_id=22, message_id=3, created_at=now, text="recent")

        self.assertEqual(store.cleanup(), 3)
        self.assertEqual(store.cleanup(), 0)
        self.assertTrue(shared.is_file())
        self.assertFalse(exclusive.exists())
        self.assertEqual(store.item_by_id(rows[11, 0]).local_media_path, str(shared))
        self.assertEqual([row.id for row in store.delivery_siblings(rows[11, 0])],
                         [rows[11, 0], rows[11, 1]])
        self.assertIsNone(store.item_by_id(rows[22, 0]))
        self.assertIsNone(store.item_by_id(rows[22, 1]))
        self.assertIsNotNone(store.item_by_id(recent))
        with closing(sqlite3.connect(store.db_path)) as db:
            indexed = set(db.execute("SELECT rowid FROM message_fts WHERE message_fts MATCH 'history'"))
            embedded = set(db.execute("SELECT message_id FROM message_embeddings"))
            expected = {(originals[11],)}
            self.assertEqual(indexed, expected)
            self.assertEqual(embedded, expected)
