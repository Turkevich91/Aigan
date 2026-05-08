import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("aigan-web")


def _safe_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only http/https URLs are allowed")

    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"}:
        raise ValueError("Refusing local/private host")

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


@mcp.tool()
def search_web(query: str, max_results: int = 5) -> str:
    """Search the public web and return titles, URLs, and snippets."""
    query = query.strip()
    if not query:
        return "Search query is empty."
    max_results = max(1, min(int(max_results), 8))

    try:
        with DDGS(timeout=12) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        return f"Search failed: {type(exc).__name__}: {exc}"

    if not results:
        return "No search results."

    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        title = item.get("title") or "(untitled)"
        href = item.get("href") or item.get("url") or ""
        body = item.get("body") or item.get("snippet") or ""
        lines.append(f"{index}. {title}\n{href}\n{body}".strip())
    return "\n\n".join(lines)


@mcp.tool()
def fetch_url(url: str, limit_chars: int = 12000) -> str:
    """Fetch and extract readable text from a public URL."""
    limit_chars = max(1000, min(int(limit_chars), 30000))
    try:
        safe_url = _safe_url(url)
    except ValueError as exc:
        return f"URL rejected: {exc}"

    headers = {"User-Agent": "aigan-mcp/1.0 (+https://modelcontextprotocol.io)"}
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
            response = client.get(safe_url)
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
