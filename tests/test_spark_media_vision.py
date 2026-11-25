"""Qualification, input boundaries and local-only dispatch for photo memory."""
import asyncio
import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from spark_media_vision import SparkMediaVision, _IMAGE_POSITION, _inline_photo


def response(text, *, actual="local-model", finish="stop"):
    return SimpleNamespace(model=actual, choices=[SimpleNamespace(
        finish_reason=finish, message=SimpleNamespace(content=text))], usage=None)


def image_url(format="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (777, 341), "red").save(buf, format=format)
    return "data:image/" + format.lower() + ";base64," + base64.b64encode(buf.getvalue()).decode()


class SparkPhotoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.create = AsyncMock()
        provider = Mock(model="local-model")
        provider.async_client.return_value.chat.completions.create = self.create
        self.vision = SparkMediaVision(provider)
        self.enterContext(patch("spark_media_vision.secrets.randbelow", return_value=234567))

    async def test_typed_position_once_only_after_native_negative_one_qualification_for_two_workers(self):
        self.create.side_effect = [response("NONE"), response("334567"), response("Червоний квадрат.")]
        self.assertEqual([True, True], await asyncio.gather(self.vision.available(), self.vision.available()))
        result = await self.vision.summarize(image_url())
        self.assertEqual("Червоний квадрат.", result.choices[0].message.content)
        blocks = [call.kwargs["messages"][0]["content"] for call in self.create.await_args_list]
        self.assertEqual([2, 3, 3], list(map(len, blocks)))
        self.assertEqual({"type": "text", "text": _IMAGE_POSITION}, blocks[-1][1])
        self.assertNotIn("334567", str([block for block in blocks[0] if block["type"] == "text"]))
        with Image.open(io.BytesIO(base64.b64decode(blocks[-1][0]["image_url"]["url"].split(",")[1]))) as img:
            self.assertEqual(("JPEG", 640), (img.format, max(img.size)))

    async def test_false_positive_plain_answer_unavailable_and_safe_retry(self):
        self.create.side_effect = [response("NONE"), response("123456")]
        self.assertFalse(await self.vision.available())
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await self.vision.summarize(image_url())
        self.assertEqual(2, self.create.await_count)
        self.create.side_effect = [response("334567"), response("wrong provider", actual="other")]
        with patch("spark_media_vision.monotonic", return_value=self.vision._refresh_at + 1):
            self.assertTrue(await self.vision.available())
            with self.assertRaisesRegex(ValueError, "incomplete"):
                await self.vision.summarize(image_url())
        self.assertEqual(2, len(self.create.await_args.kwargs["messages"][0]["content"]))

    async def test_invalid_external_mislabeled_and_animated_input_never_reach_network(self):
        with self.assertRaises(ValueError):
            await self.vision.summarize("https://example.invalid/photo.jpg")
        with self.assertRaises(OSError):
            await self.vision.summarize(image_url().replace("image/png", "image/jpeg"))
        buf = io.BytesIO()
        image = Image.new("RGB", (32, 32), "red")
        image.save(buf, format="PNG", save_all=True, append_images=[Image.new("RGB", image.size, "blue")])
        with self.assertRaises(ValueError):
            _inline_photo("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
        self.create.assert_not_awaited()

    async def test_permission_rechecked_after_the_sheet_before_transmitting_user_content(self):
        self.create.side_effect = [response("334567")]
        self.assertTrue(await self.vision.available())
        content = [{"type": "input_text", "text": "request"},
                   {"type": "input_text", "text": "Approx. 1 s"},
                   {"type": "input_image", "image_url": image_url()}]
        before_call = Mock(side_effect=PermissionError("revoked"))
        with self.assertRaises(PermissionError):
            await self.vision.clip_response("rules", [{"role": "user", "content": content}], before_call=before_call)
        before_call.assert_called_once()
        self.assertEqual(1, self.create.await_count)

    async def test_cold_menu_remains_immediate_with_hung_provider_and_shutdown_cancels_probe(self):
        entered = asyncio.Event()
        async def block(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.create.side_effect = block
        for _ in range(3):
            self.assertFalse(self.vision.menu_available())
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.assertFalse(self.vision.menu_available())
        self.assertEqual(1, self.create.await_count)
        await asyncio.wait_for(self.vision.close(), timeout=1)
        self.assertIsNone(self.vision._background)

    async def test_video_uses_one_image_with_ordered_tiles_and_rejects_reserved_request_or_speech(self):
        self.create.side_effect = [response("NONE"), response("334567"), response("Result")]
        text = json.dumps({"request": "What changed?", "speech_evidence": "No audio"})
        parts = [{"type": "input_text", "text": text}]
        parts += [{"type": "input_text", "text": "Approx. 0 s"}, {"type": "input_image", "image_url": image_url()},
                  {"type": "input_text", "text": "Approx. 2 s"}, {"type": "input_image", "image_url": image_url()}]
        await self.vision.clip_response("trusted rules", [{"role": "user", "content": parts}])
        system, user = self.create.await_args.kwargs["messages"]
        self.assertIn("chronological", system["content"])
        self.assertEqual(["image_url", "text", "text"], [b["type"] for b in user["content"]])
        self.assertEqual(_IMAGE_POSITION, user["content"][1]["text"])
        self.assertEqual(text, user["content"][2]["text"])
        self.create.reset_mock()
        parts[0]["text"] = "transcript: " + _IMAGE_POSITION
        with self.assertRaisesRegex(ValueError, "reserved"):
            await self.vision.clip_response("trusted rules", [{"role": "user", "content": parts}])
        with self.assertRaisesRegex(ValueError, "reserved"):
            await self.vision.clip_response("trusted rules", _IMAGE_POSITION, schema={"type": "json_schema"})
        self.create.assert_not_awaited()
