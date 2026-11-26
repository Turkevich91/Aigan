"""Direct, bounded HTTP transport for the public video adapters."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import socket

import httpx


class PublicMediaTransferError(Exception):
    """Only fixed failure categories may leave an acquisition adapter."""


@asynccontextmanager
async def pinned_get(url: str, referrer: str | None = None, *, cookie_header: str | None = None):
    target = httpx.URL(url)
    if target.scheme != "https" or target.port not in (None, 443):
        raise PublicMediaTransferError("address")
    hostname = target.raw_host.decode("ascii")
    records = await asyncio.get_running_loop().getaddrinfo(
        hostname, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
    )
    addresses = [ipaddress.IPv4Address(record[4][0]) for record in records]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise PublicMediaTransferError("address")
    # Pin the connection while authenticating the original TLS name. The fresh
    # transport does not inherit proxies, store cookies or follow redirects.
    headers = {"Host": hostname, "Accept-Encoding": "identity"}
    if referrer:
        headers["Referer"] = referrer
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = httpx.Request(
        "GET", target.copy_with(host=str(addresses[0])), headers=headers,
        extensions={"sni_hostname": hostname},
    )
    async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
        response = await transport.handle_async_request(request)
        try:
            yield response
        finally:
            await response.aclose()


async def bounded_chunks(response, limit: int):
    header = response.headers.get("content-length")
    if header is not None and (not header.isascii() or not header.isdecimal()):
        raise PublicMediaTransferError("invalid_length")
    digits = (header.lstrip("0") or "0") if header is not None else None
    if digits and (len(digits) > 16 or int(digits) > limit):
        raise PublicMediaTransferError("size")
    length = int(digits) if digits is not None else None
    count = 0
    try:
        async for block in response.aiter_raw(chunk_size=64 * 1024):
            count += len(block)
            if count > limit:
                raise PublicMediaTransferError("size")
            yield block
    except (httpx.RemoteProtocolError, httpx.ReadError):
        raise PublicMediaTransferError("incomplete") from None
    if count == 0 or (length is not None and count != length):
        raise PublicMediaTransferError("incomplete")
