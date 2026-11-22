"""Fetch a business's menu or price list from a link they paste.

The owner gives a URL; we download it and hand it to the catalog reader
(`catalog_import.read_catalog`) as page text, a PDF or an image. Because the server fetches
an address a stranger typed, every hop is checked: http(s) only, standard ports, no
credentials in the URL, and the host must resolve only to public internet addresses
(no loopback, private, link-local or cloud-metadata ranges). Redirects are followed by
hand so each hop gets the same check. Downloads are capped in size and time.

Known gap: the address is checked when we resolve it, and the HTTP client resolves it
again when it connects, so a DNS server that answers differently the second time
("rebinding") could slip past. Acceptable for the pilot; see docs/DESIGN.md section 17.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .catalog_import import IMAGE_TYPES, PDF_TYPE, TEXT_TYPE, ImportError_

MAX_BYTES = 8_000_000
MAX_TEXT_CHARS = 200_000
MAX_REDIRECTS = 4
TIMEOUT = 20.0
MIN_PRICES = 3  # fewer prices than this in the page text: the menu is probably drawn by scripts
USER_AGENT = "Mozilla/5.0 (compatible; MenuReader/1.0)"

_PRICE = re.compile(r"\$\s?\d+(?:\.\d\d)?|\b\d+\.\d\d\b")


def check_url(url: str) -> httpx.URL:
    """Syntax checks; raises ImportError_ with a message for the owner."""
    url = (url or "").strip()
    if not url:
        raise ImportError_("Paste the link to your menu.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    if len(url) > 2000:
        raise ImportError_("That link is too long.")
    try:
        parts = urlsplit(url)
        port = parts.port
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError
        target = httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        raise ImportError_("That doesn't look like a web link. It should start with https://")
    if parts.username or parts.password:
        raise ImportError_("Use a link without a username or password in it.")
    if port not in (None, 80, 443):
        raise ImportError_("That link uses an unusual port. Use the page's normal web address.")
    return target


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def check_host(url: httpx.URL, resolve=resolve_host) -> None:
    """Every address the host resolves to must be on the public internet."""
    host = url.host
    try:
        addrs = [host] if _is_ip(host) else await resolve(host, url.port or (443 if url.scheme == "https" else 80))
    except (OSError, UnicodeError):
        raise ImportError_("We couldn't find that website. Check the link.")
    if not addrs:
        raise ImportError_("We couldn't find that website. Check the link.")
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise ImportError_("That link isn't a public website.")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def fetch(url: str, http: httpx.AsyncClient | None = None, resolve=resolve_host) -> tuple[str, bytes]:
    """Download the link. Returns (media_type, data) ready for `read_catalog`: page text as
    text/plain, or a PDF or image as is."""
    target = check_url(url)
    own = http is None
    http = http or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                     headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,image/*;q=0.8,*/*;q=0.5"})
    try:
        for _ in range(MAX_REDIRECTS + 1):
            await check_host(target, resolve)
            try:
                async with asyncio.timeout(TIMEOUT):
                    async with http.stream("GET", target) as r:
                        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                            target = check_url(urljoin(str(target), r.headers["location"]))
                            continue
                        if r.status_code >= 400:
                            raise ImportError_(f"That page didn't load (the website answered {r.status_code}). "
                                               "Check the link, or upload photos or a PDF instead.")
                        if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                            raise ImportError_("That page is too large to read. Upload photos or a PDF instead.")
                        body = bytearray()
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) > MAX_BYTES:
                                raise ImportError_("That page is too large to read. Upload photos or a PDF instead.")
                        media_type = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                        return _to_input(media_type, bytes(body), r.encoding or "utf-8")
            except (httpx.HTTPError, TimeoutError):
                raise ImportError_("We couldn't open that link. Check it, or upload photos or a PDF instead.")
        raise ImportError_("That link redirects too many times.")
    finally:
        if own:
            await http.aclose()


def _to_input(media_type: str, body: bytes, encoding: str) -> tuple[str, bytes]:
    if media_type == PDF_TYPE or body[:5] == b"%PDF-":
        return PDF_TYPE, body
    if media_type in IMAGE_TYPES:
        return media_type, body
    if media_type in ("text/html", "application/xhtml+xml", "text/plain", ""):
        raw = body.decode(encoding, errors="replace")
        text = page_text(raw) if media_type != "text/plain" else raw
        text = text[:MAX_TEXT_CHARS]
        if len(_PRICE.findall(text)) < MIN_PRICES:
            raise ImportError_("We couldn't find prices on that page; the menu may be drawn by scripts or shown "
                               "as pictures. Upload photos or a PDF of it instead.")
        return TEXT_TYPE, text.encode()
    raise ImportError_("That link isn't a web page, PDF or photo we can read.")


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "header", "footer", "ul", "ol", "table", "dt", "dd", "option", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        if tag == "img" and not self.skip:  # alt text sometimes names the dish
            alt = dict(attrs).get("alt")
            if alt:
                self.out.append(f" {alt} ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def page_text(raw_html: str) -> str:
    """Visible text of a page, one block per line."""
    p = _TextParser()
    try:
        p.feed(raw_html)
        p.close()
    except Exception:  # malformed markup: keep what we got
        pass
    lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(p.out).split("\n"))
    return "\n".join(line for line in lines if line)
