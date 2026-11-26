"""Sensity v2 wire contract and authorization checks using a synthetic transport."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, call, patch

import httpx

import sensity_media as sensity


SLOT_ID = "00000000-0000-4000-8000-000000000001"
UPLOAD = "https://storage.googleapis.com/example/attachment?signature=synthetic"
SLOT = {"submission_id": SLOT_ID, "upload_url": UPLOAD, "status": "pending_upload"}
CONTENT = b"synthetic media"


def report(payload):
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))


class SensityMediaTests(IsolatedAsyncioTestCase):
    async def run_check(self, responses, *, permission=lambda: True, mime="image/jpeg", data=CONTENT):
        self.requests = []
        responses = iter(responses)

        def respond(request):
            self.requests.append(request)
            return next(responses)

        with patch.object(httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(respond)):
            return await sensity.check_sensity_media(data, mime, "synthetic-key", is_allowed=permission)

    async def test_upload_poll_and_report_only_verdict(self):
        for mime, extension, verdict in [
            ("image/jpeg", "jpg", "suspicious"),
            ("video/mp4", "mp4", "valid"),
            ("Audio/Ogg; codecs=opus", "ogg", "valid"),
        ]:
            with self.subTest(mime=mime), patch.object(asyncio, "sleep", new_callable=AsyncMock) as sleep:
                result = await self.run_check([
                    report(SLOT), httpx.Response(200), httpx.Response(202),
                    report({"status": "ingesting"}), report({"status": "in_progress"}),
                    report({"status": "completed", "result": verdict, "tasks": ["synthetic-private"]}),
                ], mime=mime)
                self.assertEqual(result, sensity._RESULTS[verdict])
                self.assertEqual(sleep.await_args_list, [call(3), call(6)])
                create, upload, complete, *reads = self.requests
                self.assertEqual(json.loads(create.content), {
                    "filename": f"attachment.{extension}", "product_group": "deepfake_detection",
                })
                self.assertEqual(str(create.url), "https://api.sensity.ai/v2/submissions/upload/")
                self.assertEqual((upload.method, str(upload.url), upload.content), ("PUT", UPLOAD, CONTENT))
                self.assertEqual(upload.headers["Content-Type"], "application/octet-stream")
                self.assertNotIn("apikey", upload.headers)
                path = f"https://api.sensity.ai/v2/submissions/{SLOT_ID}/"
                self.assertEqual((complete.method, str(complete.url)), ("POST", path + "complete/"))
                self.assertEqual([(q.method, str(q.url)) for q in reads], [("GET", path)] * 3)
                for request in (create, complete, *reads):
                    self.assertEqual(request.headers["apikey"], "synthetic-key")

    async def test_permission_is_checked_before_each_request_and_before_returning_result(self):
        for number_sent in range(5):
            with self.subTest(number_sent=number_sent):
                permission = Mock(side_effect=[True] * number_sent + [False])
                result = await self.run_check([
                    report(SLOT), httpx.Response(200), httpx.Response(202),
                    report({"status": "completed", "result": "valid"}),
                ], permission=permission)
                self.assertIn("зупинено", result)
                self.assertEqual(len(self.requests), number_sent)

    async def test_invalid_input_or_missing_key_never_uploads(self):
        for data, mime in [(b"", "image/jpeg"), ("https://example.org/file", "image/png"),
                           (b"x", "text/plain"), (b"x" * (sensity.SENSITY_MAX_BYTES + 1), "audio/ogg")]:
            with self.subTest(mime=mime):
                await self.run_check([], data=data, mime=mime)
                self.assertFalse(self.requests)
        with patch.object(httpx, "AsyncHTTPTransport") as transport:
            self.assertIn("ключ", await sensity.check_sensity_media(CONTENT, "audio/wav", "", is_allowed=lambda: True))
            transport.assert_not_called()

    async def test_invalid_slot_never_sends_file(self):
        for slot in [{**SLOT, "upload_url": "https://example.org/secret"},
                     {**SLOT, "upload_url": UPLOAD.replace("https:", "http:")},
                     {**SLOT, "upload_url": UPLOAD.replace("https://", "https://user@")},
                     {**SLOT, "submission_id": "../synthetic-private"}]:
            with self.subTest(invalid_field=slot != SLOT):
                result = await self.run_check([report(slot)])
                self.assertIn("Не вдалося", result)
                self.assertEqual(len(self.requests), 1)

    async def test_nonverdicts_do_not_leak_provider_content(self):
        for payload, text in [({"status": "failed"}, "не зміг"), ({"status": "skipped"}, "пропустив"),
                              ({"status": "completed", "result": "synthetic-private"}, "Не вдалося"),
                              ({"status": "synthetic-private"}, "Не вдалося")]:
            with self.subTest(text=text):
                result = await self.run_check([report(SLOT), httpx.Response(200), httpx.Response(202), report(payload)])
                self.assertIn(text, result)
                self.assertNotIn("synthetic-private", result)

    async def test_failed_or_redirecting_upload_never_starts_analysis(self):
        for code in [307, 403, 429]:
            with self.subTest(code=code):
                result = await self.run_check([
                    report(SLOT), httpx.Response(code, headers={"Location": "https://example.org/private"},
                                                text="synthetic-private"),
                ])
                self.assertEqual(len(self.requests), 2)
                self.assertNotIn("synthetic-private", result)

    async def test_pending_submission_has_one_deadline_and_no_resubmission(self):
        with patch.object(sensity, "_DEADLINE_SECONDS", 0.05):
            result = await self.run_check([
                report(SLOT), httpx.Response(200), httpx.Response(202), report({"status": "pending"}),
            ])
        self.assertIn("Час очікування", result)
        self.assertEqual([request.method for request in self.requests], ["POST", "PUT", "POST", "GET"])
