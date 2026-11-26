"""Reference boundaries, depicted-place rendering and isolated vision dispatch."""
import base64
import io
import logging
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

import public_landmarks as landmarks


def page(title="Test Bridge", qid="Q11", text="Example Bay contains Test Hall and Test Bridge.", lon=1.0):
    return {"ns": 0, "title": title, "fullurl": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
            "pageprops": {"wikibase_item": qid}, "extract": text, "coordinates": [
                {"primary": True, "globe": "earth", "lat": 1.0, "lon": lon}]}


def response(*pages):
    return {"query": {"pages": list(pages)}}


def observation(**changes):
    values = dict(medium="direct_scene", public_subject="Bridge and hall", depicted_place="Example Bay",
                  clues=["An arch and a dome"], article_titles=["Test Bridge", "Test Hall"],
                  corroborated_scene=True)
    return landmarks.LandmarkObservation(**(values | changes))


def photo():
    stream = io.BytesIO()
    im = Image.new("RGB", (100, 10), "red")
    exif = Image.Exif()
    exif[270] = "private file metadata"
    im.save(stream, format="JPEG", exif=exif)
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()


class PublicLandmarksTests(unittest.IsolatedAsyncioTestCase):
    def test_record_rejects_unusable_or_forged_references(self):
        for update in (
            {"ns": 1}, {"missing": True}, {"extract": ""}, {"pageprops": {"disambiguation": "", "wikibase_item": "Q9"}},
            {"pageprops": {"wikibase_item": "invalid"}}, {"coordinates": []},
            {"coordinates": [{"primary": True, "globe": "mars", "lat": 1, "lon": 1}]},
            {"coordinates": [{"primary": True, "globe": "earth", "lat": 91, "lon": 1}]},
            {"fullurl": "https://example.com/wiki/Test_Bridge"},
            {"fullurl": "https://en.wikipedia.org/wiki/Other"},
        ):
            with self.subTest(update=update):
                self.assertIsNone(landmarks._record(response(page() | update)))
        self.assertIsNone(landmarks._record(response(page(), page())))

    async def test_lookup_uses_one_title_and_caches_capped_requests(self):
        calls = []

        def handler(request):
            calls.append(request.url.params["titles"])
            self.assertNotIn("search", str(request.url))
            return httpx.Response(200, json=response(page(calls[-1])))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            refs = landmarks._References(client)
            for invalid in ("Bad|Extra", "User:Name", "https://example.com", "query\nline"):
                await refs.read(invalid)
            self.assertEqual([], calls)
            await refs.read("Test Bridge")
            await refs.read("test bridge")
            await refs.read("Test Hall")
            await refs.read("Third Place")
            self.assertEqual(["Test Bridge", "Test Hall"], calls)
            with patch("public_landmarks.tool_allowed", return_value=False):
                self.assertNotIn("Example Bay", await refs.read("Test Bridge"))

    async def test_rate_limit_and_oversized_response_report_source_outage(self):
        for remote in (httpx.Response(429), httpx.Response(200, content=b"x" * 65536),
                       httpx.Response(200, json={"error": "unexpected"})):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: remote)) as client:
                refs = landmarks._References(client)
                self.assertIn("unavailable", await refs.read("Test Bridge"))
                answer = await landmarks._render(observation(), refs, english=True)
                self.assertIn("reference service was unavailable", answer)
                self.assertNotIn("Example Bay", answer)

    async def test_address_candidates_never_reach_network_or_output_clues(self):
        attempted = []
        def handler(request):
            attempted.append(str(request.url))
            return httpx.Response(200, json=response(page()))

        examples = ("42 Example Street", "Example Street 42B", "вул. Зразкова, ４２", "Apt. 4",
                    "Example Street, Unit B", "Forty-two Example Road", "Example Lane Forty")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            refs = landmarks._References(client)
            for address in examples:
                with self.subTest(address=address):
                    await refs.read(address)
                    self.assertIsNone(await refs.fetch(address, settlement=True))
                    rendered = await landmarks._render(observation(clues=[address]), refs, True)
                    self.assertNotIn(address, rendered)
            self.assertEqual([], attempted)
            self.assertTrue(landmarks._title("Sydney Opera House"))
            self.assertTrue(landmarks._title("One World Trade Center"))
            self.assertFalse(landmarks._title("Taipei 101"))

    async def test_render_corroborates_only_a_direct_colocated_pair(self):
        requests = []

        def city_record(request):
            requests.append(request.url.params["titles"])
            self.assertEqual("Template:Infobox settlement", request.url.params["tltemplates"])
            city = page("Example Bay", "Q13", lon=1.1) | {"templates": [
                {"ns": 10, "title": "Template:Infobox settlement"}]}
            return httpx.Response(200, json=response(city))

        async with httpx.AsyncClient(transport=httpx.MockTransport(city_record)) as client:
            refs = landmarks._References(client)
            bridge = landmarks._record(response(page()))
            hall = landmarks._record(response(page("Test Hall", "Q12", lon=1.01)))
            refs.records = {r.title.casefold(): r for r in (bridge, hall)}
            text = await landmarks._render(observation(), refs, english=True)
            self.assertIn("near Example Bay", text)
            self.assertIn("https://en.wikipedia.org/wiki/Example_Bay", text)
            for medium in ("depiction", "uncertain"):
                text = await landmarks._render(observation(medium=medium), refs, english=True)
                self.assertIn("Where this image was taken is unknown", text)
                self.assertNotIn("Example Bay", text)
            for changes in (dict(article_titles=["Test Bridge"]), dict(article_titles=["fake", "Test Hall"]),
                            dict(depicted_place="Unknown City"), dict(corroborated_scene=False)):
                text = await landmarks._render(observation(**changes), refs, english=True)
                self.assertIn("unverified", text)
                self.assertNotIn("Unknown City", text)
                self.assertNotIn("Example Bay", text)
            far = page("Test Hall", "Q12", lon=140)
            refs.records["test hall"] = landmarks._record(response(far))
            self.assertNotIn("Example Bay", await landmarks._render(observation(), refs, english=True))
            refs.records["test hall"] = landmarks._record(response(page("Test Hall", "Q11")))
            self.assertNotIn("Example Bay", await landmarks._render(observation(), refs, english=True))
            self.assertEqual(["Example Bay"], requests)

    async def test_geographic_name_needs_a_distinct_nearby_settlement_record(self):
        settlement = page("Paris", "Q13") | {"templates": [{"ns": 10, "title": "Template:Infobox settlement"}]}
        far = settlement | {"coordinates": [{"primary": True, "globe": "earth", "lat": 48.86, "lon": 2.35}]}
        for answer in (httpx.Response(200, json=response(far)),
                       httpx.Response(200, json=response(settlement | {"templates": []})),
                       httpx.Response(200, json=response(settlement | {"pageprops": {"wikibase_item": "Q12"}})),
                       httpx.Response(200, json=response(settlement | {"pageprops": {"disambiguation": ""}})),
                       httpx.Response(503)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: answer)) as client:
                refs = landmarks._References(client)
                text = "The Paris resort features Test Bridge and Test Hall."
                refs.records = {k.casefold(): landmarks._record(response(page(k, q, text))) for k,q in (
                    ("Test Bridge", "Q11"), ("Test Hall", "Q12"))}
                rendered = await landmarks._render(observation(depicted_place="Paris"), refs, english=True)
                self.assertIn("unverified", rendered)
                self.assertNotIn("Paris", rendered)
                self.assertEqual(2, rendered.count("https://"))
                self.assertEqual(answer.status_code == 503, "service was unavailable" in rendered)

    async def test_sydney_public_pair_resolves_the_city_after_two_landmark_calls(self):
        records = {
            "Sydney Opera House": ("Q45178", -33.85681, 151.21514, "The Sydney Opera House stands near the Sydney Harbour Bridge."),
            "Sydney Harbour Bridge": ("Q54495", -33.85222, 151.21056, "The Sydney Harbour Bridge spans Sydney Harbour."),
            "Sydney": ("Q3130", -33.86778, 151.21, "Sydney is a city in Australia."),
        }
        calls = []

        def reader(request):
            title = request.url.params["titles"]
            calls.append(title)
            qid, lat, lon, introduction = records[title]
            result = page(title, qid, introduction, lon)
            result["coordinates"][0]["lat"] = lat
            if "tltemplates" in request.url.params:
                result["templates"] = [{"ns": 10, "title": "Template:Infobox settlement"}]
            return httpx.Response(200, json=response(result))

        async with httpx.AsyncClient(transport=httpx.MockTransport(reader)) as client:
            refs = landmarks._References(client)
            names = ["Sydney Opera House", "Sydney Harbour Bridge"]
            for name in names:
                await refs.read(name)
            rendered = await landmarks._render(observation(article_titles=names, depicted_place="Sydney"), refs, True)
            self.assertIn("near Sydney.", rendered)
            self.assertEqual(3, rendered.count("https://en.wikipedia.org/wiki/"))
            self.assertEqual(names + ["Sydney"], calls)

    async def test_render_permits_only_verified_source_links(self):
        async with httpx.AsyncClient() as client:
            refs = landmarks._References(client)
            refs.records["test bridge"] = landmarks._record(response(page()))
            obs = observation(public_subject='<a href="https://example.com">click</a>',
                              clues=["42.123, 12.456", "visit example.com"], article_titles=["Test Bridge"])
            text = await landmarks._render(obs, refs, english=True)
            self.assertNotIn("example.com", text)
            self.assertNotIn("42.123", text)
            self.assertNotIn("<a ", text)
            self.assertEqual(1, text.count("https://"))
            self.assertIn("https://en.wikipedia.org/wiki/Test_Bridge", text)
            text = await landmarks._render(observation(public_subject="Bridge & Hall"), refs, english=True)
            self.assertIn("Bridge & Hall", text)
            self.assertNotIn("&amp;", text)

    def test_pixels_strip_metadata_and_reject_untrusted_file_sources(self):
        encoded = landmarks._pixels(photo()).split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertFalse(image.getexif())
        for invalid in ("cid:sample-image", "https://example.com/a", "data:image/png;base64,eA=="):
            with self.assertRaises((ValueError, OSError)):
                landmarks._pixels(invalid)

    async def test_sdk_call_uses_explicit_language_disables_tracing_and_rechecks_policy(self):
        with patch.object(landmarks._debug, "DONT_LOG_MODEL_DATA", False), \
                patch.object(landmarks._debug, "DONT_LOG_TOOL_DATA", False), \
                patch.object(logging.getLogger("openai.agents"), "isEnabledFor", return_value=False):
            async def fake_run(agent, contents, *, hooks, max_turns, run_config):
                self.assertEqual("test-model", agent.model)
                self.assertFalse(agent.model_settings.store)
                self.assertEqual("low", agent.model_settings.reasoning.effort)
                self.assertTrue(run_config.tracing_disabled)
                self.assertFalse(run_config.trace_include_sensitive_data)
                self.assertIn("English", contents[0]["content"][0]["text"])
                self.assertTrue(contents[0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))
                return type("Result", (), {"final_output": observation()})()
            with patch.object(landmarks.Runner, "run", side_effect=fake_run) as run:
                text = await landmarks.analyze_public_landmark(photo(), model="test-model", effort="low", english=True)
                self.assertIn("unverified", text)
                run.assert_awaited_once()
            with patch.object(landmarks, "tool_allowed", side_effect=(True, True, False)), \
                    patch.object(landmarks.Runner, "run", side_effect=fake_run):
                text = await landmarks.analyze_public_landmark(photo(), model="test-model", effort="low", english=True)
                self.assertIn("couldn’t verify", text)
        with patch.object(landmarks._debug, "DONT_LOG_MODEL_DATA", False), \
                patch.object(logging.getLogger("openai.agents"), "isEnabledFor", return_value=True), \
                patch.object(landmarks.Runner, "run") as run:
            await landmarks.analyze_public_landmark(photo(), model="test-model", effort="low")
            run.assert_not_called()
