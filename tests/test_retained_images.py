"""Retained image authority, byte bounds and actual SDK multimodal transport."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from agents import Agent, Runner, RunConfig, _debug
from agents.tool import ToolOutputImage
from agent_capabilities import PrimaryCapabilities
from chat_history import ChatHistorySession
from history_citations import HistoryCitationSession
from memory import MemoryStore
from PIL import Image
from provenance import make_tool_provenance, tool_result_text_for_observation
from retained_images import RetainedImageSession
from tests.test_agent_capabilities import ScriptedModel
from tests import test_agent_capabilities as capability_test_support
from tests.test_standard_tools import NoTransportMCP


class RetainedImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.now = datetime.now(timezone.utc)
        self.source = self.save(10, "Synthetic source", attachment_type="photo", mime_type="image/png")
        self.path = self.cache(self.source)
        self.bot = self.save(20, "Synthetic explanation", is_bot=True, reply_to_message_id=10)
        self.current = self.save(30, "What does the small label say?", reply_to_message_id=20, created_at=self.now)

    def save(self, message_id, text, **kwargs):
        values = dict(chat_id=-1001, message_id=message_id, text=text, user_id=1,
                      sender_label="Synthetic participant", created_at=self.now - timedelta(minutes=2))
        values.update(kwargs)
        return self.store.save_message(**values)

    def cache(self, evidence_id, color="red"):
        item = self.store.item_by_id(evidence_id)
        path = self.store.media_dir / str(item.chat_id) / f"{evidence_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (12, 8), color=color).save(path)
        self.store.update_media(evidence_id, telegram_file_id="synthetic-file", attachment_type="photo", local_media_path=str(path),
                                mime_type="image/png")
        return path

    def session(self, **kwargs):
        values = dict(chat_id=-1001, cutoff_memory_id=self.current,
                      cutoff_created_at=self.now.isoformat(), reply_message_id=20)
        values.update(kwargs)
        return RetainedImageSession(self.store, **values)

    def assert_pixels(self, result):
        self.assertIsInstance(result, list)
        self.assertEqual(2, len(result))
        self.assertIsInstance(result[1], ToolOutputImage)
        self.assertTrue(result[1].image_url.startswith("data:image/png;base64,"))

    def test_explicit_reply_and_deeper_chain_select_source_on_demand(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            session = self.session()
            self.assertEqual([self.source], [x["evidence_id"] for x in session.candidates])
            self.assert_pixels(session.inspect(self.source))
        self.assertNotIn(str(self.path), json.dumps(session.candidates))
        second_bot = self.save(40, "Another explanation", is_bot=True, reply_to_message_id=30, created_at=self.now)
        last = self.save(50, "Still the same picture", reply_to_message_id=40, created_at=self.now)
        self.assert_pixels(self.session(cutoff_memory_id=last, reply_message_id=40).inspect(self.source))

    def test_outbound_provenance_reaches_trigger_without_explicit_bot_reply(self):
        output = self.save(40, "Bot output", is_bot=True)
        self.store.record_provenance_output(run_id="a"*32, chat_id=-1001, trigger_message_id=10,
            input_memory_id=self.source, route="referenced_visual_analysis", started_at=self.now,
            output_memory_id=output, output_ordinal=0, output_part_count=1)
        cutoff = self.save(50, "Read the image", created_at=self.now)
        self.assert_pixels(self.session(cutoff_memory_id=cutoff, reply_message_id=40).inspect(self.source))

    def test_source_summary_completion_does_not_invalidate_linked_pixels(self):
        session = self.session()
        self.store.update_vision_summary(self.source, "New neutral caption")
        self.assert_pixels(session.inspect(self.source))
        self.assertEqual("New neutral caption", self.store.item_by_id(self.source).vision_summary)

    def test_source_content_or_media_identity_change_invalidates_candidate(self):
        for column, value in (("text", "Changed source"), ("telegram_unique_id", "changed"),
                              ("local_media_path", str(self.path)+".other")):
            with self.subTest(column=column):
                session = self.session()
                original = getattr(self.store.item_by_id(self.source), column)
                self.store._conn.execute(f"UPDATE messages SET {column}=? WHERE id=?", (value, self.source))
                self.store._conn.commit()
                self.assertIn("image_evidence_unavailable", session.inspect(self.source))
                self.store._conn.execute(f"UPDATE messages SET {column}=? WHERE id=?", (original, self.source))
                self.store._conn.commit()

    def test_unknown_foreign_future_and_current_evidence_denied(self):
        foreign = self.save(5, "Foreign", chat_id=-2002, attachment_type="photo", mime_type="image/png")
        self.cache(foreign)
        for evidence_id in (True, 0, -1, 99999, foreign, self.current):
            with self.subTest(evidence_id=evidence_id):
                self.assertIn("image_evidence_unavailable", self.session().inspect(evidence_id))
        self.assertEqual((), self.session(reply_message_id=30).candidates)
        self.assertEqual((), self.session(reply_message_id=99999).candidates)

    def test_no_chronological_neighbor_and_bounded_reply_depth(self):
        self.assertEqual((), self.session(reply_message_id=None).candidates)
        for n in range(40, 110, 10):
            self.save(n, "Chain", reply_to_message_id=n-10)
        cutoff = self.save(120, "Follow-up", created_at=self.now)
        self.assertEqual((), self.session(cutoff_memory_id=cutoff, reply_message_id=100).candidates)

    def test_history_and_preloaded_citations_require_actual_unchanged_exposure(self):
        history = ChatHistorySession(self.store, chat_id=-1001, cutoff_memory_id=self.current,
                                      cutoff_created_at=self.now.isoformat())
        session = self.session(reply_message_id=None, history=history)
        self.assertIn("unavailable", session.inspect(self.source))
        asyncio.run(history.aread(mode="around", anchor_id=self.source))
        self.assert_pixels(session.inspect(self.source))
        citations = HistoryCitationSession(self.store, chat_id=-1001, chat_type="supergroup",
            cutoff_memory_id=self.current, cutoff_created_at=self.now.isoformat())
        line = citations.decorate_context(self.store.item_by_id(self.source), "Synthetic image source")
        session = self.session(reply_message_id=None, citations=citations)
        self.assertIn("unavailable", session.inspect(self.source))
        citations.expose_contexts([line])
        self.assert_pixels(session.inspect(self.source))
        self.store.update_vision_summary(self.source, "Edited exposed evidence")
        self.assertIsNone(citations.validated_exposed_item(self.source))

    def test_media_identity_replacement_invalidates_history_and_citation_exposure(self):
        history = ChatHistorySession(self.store, chat_id=-1001, cutoff_memory_id=self.current,
                                    cutoff_created_at=self.now.isoformat())
        history_output = asyncio.run(history.aread(mode="around", anchor_id=self.source))
        citations = HistoryCitationSession(self.store, chat_id=-1001, chat_type="supergroup",
            cutoff_memory_id=self.current, cutoff_created_at=self.now.isoformat(), history=history)
        line = citations.decorate_context(self.store.item_by_id(self.source), "Synthetic image source")
        citations.expose_contexts([line])
        self.assertNotIn(str(self.path), history_output + line)
        self.assertNotIn("synthetic-file", history_output + line)
        for column, value in (("telegram_file_id", "replacement"), ("telegram_unique_id", "replacement"),
                              ("mime_type", "image/jpeg"), ("local_media_path", str(self.path)+".replacement")):
            with self.subTest(column=column):
                before = getattr(self.store.item_by_id(self.source), column)
                self.store._conn.execute(f"UPDATE messages SET {column}=? WHERE id=?", (value, self.source))
                self.store._conn.commit()
                self.assertIsNone(history.validated_exposed_item(self.source))
                self.assertIsNone(citations.validated_exposed_item(self.source))
                self.assertIn("unavailable", self.session(reply_message_id=None, history=history).inspect(self.source))
                self.assertIn("unavailable", self.session(reply_message_id=None, citations=citations).inspect(self.source))
                self.store._conn.execute(f"UPDATE messages SET {column}=? WHERE id=?", (before, self.source))
                self.store._conn.commit()

    def test_citation_reader_uses_exact_validated_snapshot(self):
        citations = HistoryCitationSession(self.store, chat_id=-1001, chat_type="supergroup",
            cutoff_memory_id=self.current, cutoff_created_at=self.now.isoformat())
        line = citations.decorate_context(self.store.item_by_id(self.source), "Synthetic image source")
        citations.expose_contexts([line])
        session = self.session(reply_message_id=None, citations=citations)
        original = citations.validated_exposed_item
        validated = []
        def validate_after_update(item_id):
            # This operational update is outside source identity; the resolver still
            # must return its actual validated object rather than an earlier fetch.
            self.store._conn.execute("UPDATE messages SET raw_note=? WHERE id=?", ("updated note", item_id))
            self.store._conn.commit()
            item = original(item_id)
            validated.append(item)
            return item
        with patch.object(citations, "validated_exposed_item", side_effect=validate_after_update), \
                patch.object(session, "_read_cache", wraps=session._read_cache) as reader:
            self.assert_pixels(session.inspect(self.source))
        self.assertIs(reader.call_args.args[0], validated[0])
        self.assertEqual("updated note", reader.call_args.args[0].raw_note)

    def test_history_source_swap_after_validation_is_rejected_before_cache_read(self):
        for through_citations in (False, True):
            with self.subTest(through_citations=through_citations):
                history = ChatHistorySession(self.store, chat_id=-1001, cutoff_memory_id=self.current,
                                            cutoff_created_at=self.now.isoformat())
                asyncio.run(history.aread(mode="around", anchor_id=self.source))
                citations = (HistoryCitationSession(self.store, chat_id=-1001, chat_type="supergroup",
                    cutoff_memory_id=self.current, cutoff_created_at=self.now.isoformat(), history=history)
                    if through_citations else None)
                session = self.session(reply_message_id=None, citations=citations,
                                       history=None if through_citations else history)
                original = history.validated_exposed_item
                def validate_then_replace(item_id):
                    evidence = original(item_id)
                    self.store._conn.execute("UPDATE messages SET telegram_unique_id=? WHERE id=?",
                                             (f"replacement-{through_citations}", item_id))
                    self.store._conn.commit()
                    return evidence
                with patch.object(history, "validated_exposed_item", side_effect=validate_then_replace), \
                        patch.object(session, "_read_cache") as reader:
                    self.assertIn("image_evidence_unavailable", session.inspect(self.source))
                reader.assert_not_called()

    def test_validated_history_source_rechecks_chat_after_registry_validation(self):
        history = ChatHistorySession(self.store, chat_id=-1001, cutoff_memory_id=self.current,
                                    cutoff_created_at=self.now.isoformat())
        asyncio.run(history.aread(mode="around", anchor_id=self.source))
        original = history.validated_exposed_item
        def validate_then_move(item_id):
            evidence = original(item_id)
            self.store._conn.execute("UPDATE messages SET chat_id=-2002 WHERE id=?", (item_id,))
            self.store._conn.commit()
            return evidence
        with patch.object(history, "validated_exposed_item", side_effect=validate_then_move):
            self.assertIsNone(history.validated_exposed_source(self.source))

    def test_missing_oversized_invalid_mime_and_truncated_cache_fail_truthfully(self):
        self.assertIn("image_cache_unavailable", self.session(max_bytes=10).inspect(self.source))
        self.path.write_bytes(b"not an image")
        self.assertIn("image_cache_unavailable", self.session().inspect(self.source))
        self.cache(self.source)
        self.store.update_media(self.source, telegram_file_id="synthetic-file", attachment_type="photo", mime_type="image/jpeg", local_media_path=str(self.path))
        self.assertIn("image_cache_unavailable", self.session().inspect(self.source))
        self.path.unlink()
        self.assertIn("image_cache_unavailable", self.session().inspect(self.source))

    def test_decoder_pixel_frame_and_exception_bounds(self):
        with patch("retained_images.MAX_PIXELS", 50):
            self.assertIn("image_cache_unavailable", self.session().inspect(self.source))
        gif = self.path.with_suffix(".gif")
        Image.new("RGB", (12, 8), "red").save(gif, save_all=True,
            append_images=[Image.new("RGB", (12, 8), "blue")], duration=100, loop=0)
        self.store.update_media(self.source, telegram_file_id="synthetic-file", attachment_type="photo",
                                local_media_path=str(gif), mime_type="image/gif")
        self.assertIn("image_cache_unavailable", self.session().inspect(self.source))
        session = self.session()
        with patch.object(session, "_read_cache", side_effect=RuntimeError("private-path-marker")):
            result = asyncio.run(session.ainspect(self.source))
        self.assertIn("image_cache_unavailable", result)
        self.assertNotIn("private-path-marker", result)

    def test_symlink_wrong_owner_and_external_cache_rejected(self):
        original = self.path.read_bytes()
        outside = Path(self.tmp.name)/"other.png"
        outside.write_bytes(original)
        self.path.unlink()
        self.path.symlink_to(outside)
        self.assertIn("image_cache_unavailable", self.session().inspect(self.source))
        self.path.unlink()
        self.path.write_bytes(original)
        other = self.path.with_name("999.png")
        other.write_bytes(original)
        for path in (outside, other):
            self.store.update_media(self.source, telegram_file_id="synthetic-file", attachment_type="photo", mime_type="image/png", local_media_path=str(path))
            self.assertIn("image_cache_unavailable", self.session().inspect(self.source))
        self.store.update_media(self.source, telegram_file_id="synthetic-file", attachment_type="photo", mime_type="image/png", local_media_path=str(self.path))
        owned = self.path.parent
        moved = owned.with_name("cached-original")
        owned.rename(moved)
        owned.symlink_to(moved, target_is_directory=True)
        self.assertIn("image_cache_unavailable", self.session().inspect(self.source))

    def test_dedupe_and_attempt_cap_under_parallel_calls(self):
        session = self.session()
        async def read_twice():
            return await asyncio.gather(session.ainspect(self.source), session.ainspect(self.source))
        results = asyncio.run(read_twice())
        self.assertEqual(1, sum(isinstance(value, list) for value in results))
        for _ in range(4): session.inspect(999)
        self.assertIn("image_read_limit", session.inspect(self.source))

    def test_three_unique_images_and_identical_pixel_dedupe(self):
        ids = [self.source]
        for n, color in ((11, "blue"), (12, "green"), (13, "yellow"), (14, "red")):
            row = self.save(n, "Source", attachment_type="photo", mime_type="image/png")
            self.cache(row, color); ids.append(row)
        cutoff = self.save(40, "Inspect", created_at=self.now)
        history = ChatHistorySession(self.store, chat_id=-1001, cutoff_memory_id=cutoff,
                                      cutoff_created_at=self.now.isoformat())
        asyncio.run(history.aread(mode="recent", limit=20))
        session = self.session(cutoff_memory_id=cutoff, history=history)
        self.assert_pixels(session.inspect(ids[0]))
        self.assertIn("Identical image pixels", session.inspect(ids[4]))
        self.assert_pixels(session.inspect(ids[1]))
        self.assert_pixels(session.inspect(ids[2]))
        self.assertIn("image_read_limit", session.inspect(ids[3]))

    def test_actual_sdk_returns_input_image_only_after_tool_and_preserves_read_only_state(self):
        session = self.session()
        capabilities = PrimaryCapabilities(retained_images=session)
        model = ScriptedModel([("inspect_chat_image", {"evidence_id":self.source}), "Read the small label."])
        agent = Agent(name="test", model=model, tools=capabilities.tools(),
                      tool_use_behavior=capabilities.tool_use_behavior)
        result = asyncio.run(Runner.run(agent, "Read earlier image", max_turns=6,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)))
        self.assertEqual("Read the small label.", result.final_output)
        self.assertNotIn("input_image", str(model.inputs[0]))
        output = next(x for x in model.inputs[1] if x.get("type") == "function_call_output")
        self.assertEqual("input_image", output["output"][1]["type"])
        self.assertIn("untrusted historical evidence", output["output"][0]["text"])
        self.assertEqual("", self.store.item_by_id(self.source).vision_summary)
        self.assertEqual("", self.store.item_by_id(self.current).local_media_path)

    def test_privacy_adapter_denies_sensitive_tracing_or_debug_payload_logging(self):
        async def run(config):
            capabilities = PrimaryCapabilities(retained_images=self.session())
            model = ScriptedModel([("inspect_chat_image", {"evidence_id":self.source}), "Unavailable."])
            await Runner.run(Agent(name="test", model=model, tools=capabilities.tools()),
                             "Read image", run_config=config)
            return str(model.inputs[1])
        output = asyncio.run(run(RunConfig(tracing_disabled=True, trace_include_sensitive_data=True)))
        self.assertIn("image_privacy_unavailable", output)
        self.assertNotIn("base64", output)
        with patch.object(_debug, "DONT_LOG_TOOL_DATA", False):
            output = asyncio.run(run(RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)))
        self.assertIn("image_privacy_unavailable", output)
        with patch.object(logging.getLogger("openai._base_client"), "isEnabledFor", return_value=True):
            output = asyncio.run(run(RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)))
        self.assertIn("image_privacy_unavailable", output)
        self.assertNotIn("base64", output)

    def test_observation_never_stringifies_structured_pixels(self):
        class PrivatePixels:
            def __repr__(self): raise AssertionError("Must not stringify pixels")
        output = [PrivatePixels()]
        self.assertIn("omitted", tool_result_text_for_observation("inspect_chat_image", output))
        record = make_tool_provenance("inspect_chat_image", {"evidence_id":self.source}, output,
                                      secret="synthetic", failure_classifier=main.classify_tool_result_failure)
        self.assertEqual("ok", record.result_status)
        self.assertNotIn("evidence_id", record.result_digest)
        with patch.object(main, "system_event") as event:
            asyncio.run(main.AiganRunHooks("test").on_tool_end(None, None,
                SimpleNamespace(name="inspect_chat_image"), output))
        self.assertIn("result_chars", event.call_args.kwargs["details"])

    def test_actual_sdk_metadata_trace_and_main_hooks_never_export_pixels(self):
        from agents.tracing import get_trace_provider
        from agents.tracing.provider import SynchronousMultiTracingProcessor
        from agents.models.interface import ModelTracing
        records, tracing_modes = [], []
        class Collector:
            def on_trace_start(self, trace): pass
            def on_trace_end(self, trace): records.append(trace.export())
            def on_span_start(self, span): pass
            def on_span_end(self, span): records.append(span.export())
            def force_flush(self): pass
            def shutdown(self, **kwargs): pass
        class TracedModel(ScriptedModel):
            async def get_response(self, *args, **kwargs):
                tracing_modes.append(kwargs.get("tracing", args[6] if len(args) > 6 else None))
                return await super().get_response(*args, **kwargs)
        model = TracedModel([("inspect_chat_image", {"evidence_id": self.source}), "Synthetic image answer"])
        capabilities = PrimaryCapabilities(retained_images=self.session())
        original_factory = main.make_agent
        def factory(servers, capabilities=None, **kwargs):
            return original_factory([], capabilities, **kwargs)
        processor = SynchronousMultiTracingProcessor()
        processor.add_tracing_processor(Collector())
        provider = get_trace_provider()
        with ExitStack() as stack:
            stack.enter_context(patch.object(provider, "_multi_processor", processor))
            stack.enter_context(patch.object(provider, "_manual_disabled", False))
            stack.enter_context(patch.object(provider, "_disabled", False))
            stack.enter_context(patch.object(main, "CONFIG", replace(main.CONFIG, agents_tracing_mode="sensitive")))
            stack.enter_context(patch.object(main, "configured_agents_model", return_value=model))
            stack.enter_context(patch.object(main, "MCPServerStdio", NoTransportMCP))
            stack.enter_context(patch.object(main, "make_agent", side_effect=factory))
            events = stack.enter_context(patch.object(main, "system_event"))
            result = asyncio.run(main.run_agent("Inspect the retained image", capability_context=capabilities))
        self.assertEqual("Synthetic image answer", result)
        self.assertEqual([ModelTracing.ENABLED_WITHOUT_DATA]*2, tracing_modes)
        output = next(x for x in model.inputs[1] if x.get("type") == "function_call_output")
        pixels = output["output"][1]["image_url"]
        self.assertTrue(pixels.startswith("data:image/png;base64,"))
        self.assertTrue(records)
        function_spans = [row for row in records if row.get("span_data", {}).get("type") == "function"]
        self.assertEqual(1, len(function_spans))
        self.assertIsNone(function_spans[0]["span_data"].get("output"))
        recorded = json.dumps(records) + str(events.call_args_list)
        self.assertNotIn(pixels, recorded)
        self.assertNotIn(str(self.path), recorded)
        self.assertNotIn("data:image/", recorded)

    def test_run_config_suppresses_sensitive_payloads_only_for_reader_runs(self):
        with patch.object(main, "CONFIG", replace(main.CONFIG, agents_tracing_mode="sensitive")):
            self.assertTrue(main.build_agents_run_config().trace_include_sensitive_data)
            self.assertFalse(main.build_agents_run_config(retained_image_reads=True).trace_include_sensitive_data)


class RetainedImageHostTests(unittest.TestCase):
    setUp = capability_test_support.AgentCapabilityTests.setUp
    save = capability_test_support.AgentCapabilityTests.save
    message = capability_test_support.AgentCapabilityTests.message
    run_host = capability_test_support.AgentCapabilityTests.run_host
    # Reuse existing full host-routing fixture; only this new route needs extension.
    def test_referenced_visual_analysis_without_immediate_photo_reaches_primary(self):
        self.save(10, "Synthetic source", attachment_type="photo", mime_type="image/png")
        self.save(20, "Prior image answer", is_bot=True, reply_to_message_id=10)
        message = self.message("Read that diagram", reply_id=20)
        async def primary(_provenance, prompt, *, capability_context, **kwargs):
            self.assertIsNotNone(capability_context.retained_images)
            self.assertIn("inspect_chat_image", capability_context.guidance())
            return "I can inspect the retained source."
        patches = self.run_host("referenced_visual_analysis", message, AsyncMock(side_effect=primary))
        patches["run_agent_for_outbound"].assert_awaited_once()
        patches["send_reply"].assert_awaited_once()
        patches["maybe_send_internet_image"].assert_not_awaited()
