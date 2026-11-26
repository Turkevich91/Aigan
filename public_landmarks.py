"""Identify public landmarks in one user-invoked photograph using bounded reference evidence."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import io
import json
import logging
import math
from pathlib import Path
import re
import unicodedata
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from agents import Agent, Model, ModelSettings, RunConfig, RunHooks, Runner, _debug, function_tool
import httpx
from openai.types.shared import Reasoning
from PIL import Image, ImageOps
from pydantic import BaseModel

from chat_tool_policy import tool_allowed


_POLICY = (Path(__file__).parent / "prompts" / "public_landmarks.md").read_text(encoding="utf-8")
_WIKI_API = "https://en.wikipedia.org/w/api.php"
_MIME = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP", "image/gif": "GIF"}
_UNSAFE_OUTPUT = re.compile(r"(?:https?[:/]|www\.|[\w-]+\.[a-z]{2,}\b|@\w|[<>\[\]{}\n\r]|[\d°]\s*°|\d[.,]\d{2,}|\b\d{4}\b)", re.I)


class LandmarkObservation(BaseModel):
    medium: Literal["direct_scene", "depiction", "uncertain"]
    public_subject: str | None
    depicted_place: str | None
    clues: list[str]
    article_titles: list[str]
    corroborated_scene: bool


@dataclass(frozen=True)
class LandmarkRecord:
    title: str
    qid: str
    introduction: str
    lat: float
    lon: float
    url: str


def _pixels(data_url: str) -> str:
    if not isinstance(data_url, str) or len(data_url) > 16_000_050:
        raise ValueError("invalid_photo")
    header, sep, encoded = data_url.partition(",")
    mime = header.removeprefix("data:").removesuffix(";base64")
    if not sep or mime not in _MIME or header != f"data:{mime};base64":
        raise ValueError("invalid_photo")
    data = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(data), formats=[_MIME[mime]]) as image:
        if image.width * image.height > 20_000_000 or getattr(image, "n_frames", 1) != 1:
            raise ValueError("invalid_photo")
        image = ImageOps.exif_transpose(image)
        image.thumbnail((960, 960), Image.Resampling.LANCZOS)
        clean = image.convert("RGB")
        clean.info.clear()
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _title(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 90 and len(value.split()) <= 12
            and value == value.strip() and any(c.isalpha() for c in value)
            and not _UNSAFE_OUTPUT.search(value)
            and all(c.isalnum() or c in " '-’.,()&–—" for c in value))


def _record(payload: dict) -> LandmarkRecord | None:
    pages = payload["query"]["pages"]
    if not isinstance(pages, list) or len(pages) != 1:
        return None
    page = pages[0]
    if type(page.get("ns")) is not int or page["ns"] != 0 or "missing" in page or "invalid" in page:
        return None
    title, properties = page["title"], page["pageprops"]
    if (not _title(title) or "disambiguation" in properties
            or not re.fullmatch(r"Q[1-9]\d*", properties.get("wikibase_item", ""))):
        return None
    intro = page.get("extract")
    if not isinstance(intro, str) or not intro.strip():
        return None
    url = urlsplit(page["fullurl"])
    canonical = "/wiki/" + title.replace(" ", "_")
    if (url.scheme != "https" or url.netloc != "en.wikipedia.org" or url.query or url.fragment
            or unquote(url.path) != canonical):
        return None
    for coordinate in page.get("coordinates", ()):
        if coordinate.get("primary") is not True or coordinate.get("globe") != "earth":
            continue
        lat, lon = coordinate["lat"], coordinate["lon"]
        if (type(lat) in (int, float) and type(lon) in (int, float)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            return LandmarkRecord(title, properties["wikibase_item"], intro[:2400], lat, lon,
                                  "https://en.wikipedia.org" + quote(canonical, safe="/_"))
    return None


class _References:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.attempted: set[str] = set()
        self.records: dict[str, LandmarkRecord] = {}
        self.unavailable = False

    async def fetch(self, title: str, *, settlement: bool = False) -> LandmarkRecord | None:
        if not _title(title) or not tool_allowed("web", before_call=True):
            return None
        params = dict(action="query", titles=title, redirects=1, format="json", formatversion=2,
                      prop="extracts|info|coordinates|pageprops", exintro=1, explaintext=1,
                      inprop="url", coprop="globe", ppprop="wikibase_item|disambiguation")
        if settlement:
            # On English Wikipedia national city infoboxes can transclude Infobox settlement.
            params.update(prop=params["prop"] + "|templates", tltemplates="Template:Infobox settlement", tllimit=1)
        try:
            async with self.client.stream("GET", _WIKI_API, params=params) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > 64_000:
                        raise ValueError("oversized_reference")
                    raw.extend(chunk)
            payload = json.loads(raw)
            record = _record(payload)
            if settlement and record is not None:
                templates = payload["query"]["pages"][0].get("templates", [])
                if not any(t.get("ns") == 10 and t.get("title") == "Template:Infobox settlement" for t in templates):
                    return None
            return record if tool_allowed("web", before_call=True) else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            self.unavailable = True
            return None

    async def read(self, title: str) -> str:
        if not tool_allowed("web", before_call=True) or not _title(title):
            return "No eligible public reference."
        key = title.casefold()
        if key in self.records:
            record = self.records[key]
        elif key in self.attempted or len(self.attempted) >= 2:
            return "Reference lookup limit reached."
        else:
            self.attempted.add(key)
            record = await self.fetch(title)
            if record is None:
                return "Reference service unavailable." if self.unavailable else "No eligible public reference."
            self.records[key] = self.records[record.title.casefold()] = record
        return json.dumps({"title": record.title, "reference": record.introduction}, ensure_ascii=False)


def _distance_km(a: LandmarkRecord, b: LandmarkRecord) -> float:
    r = math.radians
    haversine = (math.sin(r(a.lat - b.lat) / 2) ** 2
                 + math.cos(r(a.lat)) * math.cos(r(b.lat)) * math.sin(r(a.lon - b.lon) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1, haversine)))


def _nearby(a: LandmarkRecord, b: LandmarkRecord) -> bool:
    return (a.qid != b.qid and _distance_km(a, b) <= 5
            and (a.title.casefold() in b.introduction.casefold() or b.title.casefold() in a.introduction.casefold()))


def _label(value: str | None, limit: int) -> str:
    if (not value or len(value) > limit or _UNSAFE_OUTPUT.search(value)
            or any(unicodedata.category(c).startswith("C") or c in "*#`" for c in value)):
        return ""
    return " ".join(value.split())


async def _render(obs: LandmarkObservation, refs: _References, english: bool) -> str:
    sources = list(dict.fromkeys(refs.records[t.casefold()] for t in obs.article_titles[:2]
                                 if t.casefold() in refs.records and refs.records[t.casefold()].title == t))
    subject = _label(obs.public_subject, 120) if sources else ""
    place = _label(obs.depicted_place, 70)
    settlement = None
    if (subject and place and obs.corroborated_scene and obs.medium == "direct_scene"
            and len(sources) == 2 and _nearby(*sources) and all(
                re.search(r"(?<!\w)" + re.escape(place) + r"(?!\w)", s.introduction.split(". ", 1)[0], re.I)
                for s in sources)):
        candidate = await refs.fetch(place, settlement=True)
        if candidate and all(s.qid != candidate.qid and _distance_km(s, candidate) <= 25 for s in sources):
            settlement = candidate
            sources.append(settlement)
    lines = []
    if subject:
        lines.append(("Possible landmark: " if english else "Можлива пам’ятка: ") + subject)
    for clue in obs.clues[:2]:
        if safe := _label(clue, 120):
            lines.append(("Visible: " if english else "Видно: ") + safe)
    if settlement:
        lines.append(("The two landmarks place the depicted scene near " if english else
                      "За двома пам’ятками показане місце розташоване поблизу ") + settlement.title + ".")
    if obs.medium in {"depiction", "uncertain"}:
        lines.append("This may be a reproduction or model. Where this image was taken is unknown."
                     if english else "Це може бути репродукція або модель. Де знято цей кадр, невідомо.")
    elif settlement:
        lines.append("The camera position is unknown." if english else "Точку зйомки не встановлено.")
    else:
        lines.append("The scene’s location is unverified." if english else "Місце зйомки не підтверджене.")
    if refs.unavailable:
        lines.append("The reference service was unavailable." if english else "Довідковий сервіс був недоступний.")
    lines.extend(f"{r.title} — {r.url}" for r in sources)
    return "\n".join(lines)


async def analyze_public_landmark(image_url: str, *, model: str | Model, effort: str, english: bool = False,
                                  hooks: RunHooks | None = None) -> str:
    """Read one metadata-free image in an isolated SDK run with current-chat web authorization."""
    unavailable = ("I couldn’t verify the landmark or scene." if english else
                   "Не вдалося перевірити пам’ятку або місце зйомки.")
    sdk_logs_payload = (logging.getLogger("openai.agents").isEnabledFor(logging.DEBUG)
                        and (not _debug.DONT_LOG_TOOL_DATA or not _debug.DONT_LOG_MODEL_DATA))
    if (not tool_allowed("web", before_call=True) or sdk_logs_payload
            or logging.getLogger("openai._base_client").isEnabledFor(logging.DEBUG)):
        return unavailable
    try:
        async with asyncio.timeout(80):
            image = await asyncio.to_thread(_pixels, image_url)
            async with httpx.AsyncClient(timeout=8, headers={
                "User-Agent": "Aigan-Landmarks/1.0 (https://github.com/Turkevich91/Aigan)"
            }) as client:
                refs = _References(client)

                @function_tool
                async def read_landmark_article(title: str) -> str:
                    """Read one English Wikipedia article by exact public landmark title."""
                    return await refs.read(title)

                agent = Agent(name="Public landmark identification", model=model, instructions=_POLICY,
                              tools=[read_landmark_article], output_type=LandmarkObservation,
                              model_settings=ModelSettings(reasoning=Reasoning(effort=effort), max_tokens=1250,
                                                           store=False, parallel_tool_calls=False))
                if not tool_allowed("web", before_call=True):
                    return unavailable
                question = ("Which public landmark is depicted? Answer in English." if english else
                            "Де знято це фото? Відповідай українською.")
                result = await Runner.run(agent, [{"role": "user", "content": [
                    {"type": "input_text", "text": question}, {"type": "input_image", "image_url": image},
                ]}], hooks=hooks, max_turns=4, run_config=RunConfig(tracing_disabled=True,
                                                                    trace_include_sensitive_data=False))
                if tool_allowed("web", before_call=True):
                    answer = await _render(result.final_output, refs, english)
                    if tool_allowed("web", before_call=True):
                        return answer
    except Exception:
        # SDK/provider errors can contain request bodies. Return fixed text without logging them.
        pass
    return unavailable
