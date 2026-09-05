"""Safe public-metadata parser adapters used by the Telegram MVP.

This module intentionally does not handle cookies, signatures, login sessions,
or platform access-control bypasses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.request import Request, urlopen


@dataclass
class PublicMetadata:
    title: str | None = None
    description: str | None = None
    image: str | None = None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        data = {k.lower(): (v or "") for k, v in attrs}
        key = data.get("property") or data.get("name")
        content = data.get("content")
        if key and content:
            self.values.setdefault(key.lower(), content.strip())


def _fetch_public_metadata(url: str, timeout: float = 8.0) -> PublicMetadata:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramParserBot/1.0)"})
    with urlopen(req, timeout=timeout) as response:
        raw = response.read(512 * 1024)
    parser = _MetaParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    values = parser.values
    return PublicMetadata(
        title=values.get("og:title") or values.get("twitter:title"),
        description=values.get("og:description") or values.get("description"),
        image=values.get("og:image") or values.get("twitter:image"),
    )


async def fetch_public_metadata(url: str, timeout: float = 8.0) -> PublicMetadata:
    return await asyncio.to_thread(_fetch_public_metadata, url, timeout)
