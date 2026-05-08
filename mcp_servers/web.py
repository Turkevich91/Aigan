import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("aigan-web")
MAX_REDIRECTS = 5

BLOCKED_RUSSIAN_HOSTS = {
    "dzen.ru",
    "gazeta.ru",
    "kommersant.ru",
    "lenta.ru",
    "mail.ru",
    "rambler.ru",
    "ria.ru",
    "rt.com",
    "sputnikglobe.com",
    "tass.ru",
    "vk.com",
    "yandex.ru",
}


def _is_blocked_russian_host(host: str | None) -> bool:
    if not host:
        return False

    host = host.lower().strip(".")
    if host.endswith(".ru") or host.endswith(".su"):
        return True
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_RUSSIAN_HOSTS)


def _safe_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http/https URLs are allowed")

    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"}:
        raise ValueError("Refusing local/private host")
    if _is_blocked_russian_host(host):
        raise ValueError("Russian domains and Russian search/media services are disabled for this bot")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host: {host}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Refusing local/private network address")

    return parsed.geturl()


def _clean_html(html: str, limit_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = "\n".join(lines)
    if title and not compact.startswith(title):
        compact = f"{title}\n\n{compact}"
    return compact[:limit_chars]


def _get_with_checked_redirects(client: httpx.Client, url: str) -> httpx.Response:
    current_url = _safe_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        response = client.get(current_url)
        if not response.is_redirect:
            return response

        location = response.headers.get("location")
        if not location:
            raise ValueError("Redirect response has no Location header")
        current_url = _safe_url(urljoin(str(response.url), location))

    raise ValueError(f"Too many redirects; limit is {MAX_REDIRECTS}")


def _safe_item_url(url: str) -> str:
    try:
        return _safe_url(url)
    except ValueError:
        return ""


def search_image_candidates(query: str, max_results: int = 5, region: str = "ua-uk") -> list[dict[str, str]]:
    """Return safe public image candidates without exposing private/Russian hosts."""
    query = query.strip()
    if not query:
        return []
    max_results = max(1, min(int(max_results), 8))
    region = (region or "ua-uk").strip()

    with DDGS(timeout=12) as ddgs:
        raw_results = list(ddgs.images(query, max_results=max_results * 4, region=region))

    results: list[dict[str, str]] = []
    for item in raw_results:
        image_url = item.get("image") or item.get("thumbnail") or ""
        source_url = item.get("url") or item.get("source") or item.get("href") or image_url
        safe_image = _safe_item_url(image_url)
        safe_source = _safe_item_url(source_url) if source_url else safe_image
        if not safe_image:
            continue
        results.append(
            {
                "title": item.get("title") or item.get("body") or "(untitled image)",
                "image": safe_image,
                "source": safe_source or safe_image,
            }
        )
        if len(results) >= max_results:
            break
    return results


def fetch_binary_url(url: str, max_bytes: int = 10_000_000, allowed_content_prefixes: tuple[str, ...] = ("image/",)) -> tuple[bytes, str, str]:
    """Fetch a safe public binary URL with checked redirects and content-type limits."""
    safe_url = _safe_url(url)
    max_bytes = max(1, min(int(max_bytes), 10_000_000))
    headers = {"User-Agent": "aigan-mcp/1.0 (+https://modelcontextprotocol.io)"}
    with httpx.Client(timeout=20, follow_redirects=False, headers=headers) as client:
        response = _get_with_checked_redirects(client, safe_url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if allowed_content_prefixes and not any(content_type.startswith(prefix) for prefix in allowed_content_prefixes):
            raise ValueError(f"Unexpected content-type: {content_type or 'unknown'}")
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError(f"Remote file too large: {content_length} bytes")
        data = response.content
        if len(data) > max_bytes:
            raise ValueError(f"Remote file too large: {len(data)} bytes")
        return data, content_type or "application/octet-stream", str(response.url)


@mcp.tool()
def search_web(query: str, max_results: int = 5, region: str = "ua-uk") -> str:
    """Search the public web using Ukrainian/English-oriented results; Russian domains are filtered."""
    query = query.strip()
    if not query:
        return "Search query is empty."
    max_results = max(1, min(int(max_results), 8))
    region = (region or "ua-uk").strip()

    try:
        with DDGS(timeout=12) as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results * 3, region=region))
    except Exception as exc:
        return f"Search failed: {type(exc).__name__}: {exc}"

    results = []
    for item in raw_results:
        href = item.get("href") or item.get("url") or ""
        host = urlparse(href).hostname
        if _is_blocked_russian_host(host):
            continue
        results.append(item)
        if len(results) >= max_results:
            break

    if not results:
        return "No search results after filtering Russian domains/services."

    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        title = item.get("title") or "(untitled)"
        href = item.get("href") or item.get("url") or ""
        body = item.get("body") or item.get("snippet") or ""
        lines.append(f"{index}. {title}\n{href}\n{body}".strip())
    return "\n\n".join(lines)


@mcp.tool()
def search_images(query: str, max_results: int = 5, region: str = "ua-uk") -> str:
    """Search public images; Russian/private hosts are filtered. Returns image URLs and source pages."""
    try:
        results = search_image_candidates(query, max_results=max_results, region=region)
    except Exception as exc:
        return f"Image search failed: {type(exc).__name__}: {exc}"

    if not results:
        return "No image results after filtering Russian/private hosts."

    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        lines.append(
            "\n".join(
                [
                    f"{index}. {item['title']}",
                    f"Image: {item['image']}",
                    f"Source: {item['source']}",
                ]
            )
        )
    return "\n\n".join(lines)


@mcp.tool()
def fetch_url(url: str, limit_chars: int = 12000) -> str:
    """Fetch and extract readable text from a public non-Russian URL."""
    limit_chars = max(1000, min(int(limit_chars), 30000))
    try:
        safe_url = _safe_url(url)
    except ValueError as exc:
        return f"URL rejected: {exc}"

    headers = {"User-Agent": "aigan-mcp/1.0 (+https://modelcontextprotocol.io)"}
    try:
        with httpx.Client(timeout=20, follow_redirects=False, headers=headers) as client:
            response = _get_with_checked_redirects(client, safe_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                return _clean_html(response.text, limit_chars)
            if content_type.startswith("text/") or "json" in content_type:
                return response.text[:limit_chars]
            return f"Fetched {safe_url}, but content-type is not text: {content_type or 'unknown'}"
    except Exception as exc:
        return f"Fetch failed: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
