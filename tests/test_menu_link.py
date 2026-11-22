import asyncio

import httpx
import pytest

from phone_agent import menu_link
from phone_agent.catalog_import import ImportError_


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("url", ["ftp://a.example/menu", "https://user:pw@a.example/", "https://a.example:8080/",
                                 "", "javascript:alert(1)"])
def test_bad_links_are_refused(url):
    with pytest.raises(ImportError_):
        menu_link.check_url(url)


def test_scheme_is_added():
    assert str(menu_link.check_url("mamabros.example/menu")) == "https://mamabros.example/menu"


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                                  "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_private_addresses_are_refused(addr):
    async def resolve(host, port):
        return ["93.184.216.34", addr]  # one bad address is enough to refuse
    with pytest.raises(ImportError_, match="public"):
        run(menu_link.check_host(httpx.URL("https://menu.example/"), resolve))


def test_ip_literals_are_checked_too():
    with pytest.raises(ImportError_):
        run(menu_link.check_host(httpx.URL("http://169.254.169.254/latest/meta-data/")))
    with pytest.raises(ImportError_):
        run(menu_link.check_host(httpx.URL("http://[::1]/")))


async def _public(host, port):
    return ["93.184.216.34"]


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_redirect_to_a_private_address_is_refused():
    async def resolve(host, port):
        return {"menu.example": ["93.184.216.34"], "evil.example": ["127.0.0.1"]}[host]

    def handler(req):
        return httpx.Response(302, headers={"location": "http://evil.example/admin"})

    with pytest.raises(ImportError_, match="public"):
        run(menu_link.fetch("https://menu.example/", _client(handler), resolve))


def test_pdf_and_image_links_pass_through():
    pdf = b"%PDF-1.7 fake"
    out = run(menu_link.fetch("https://m.example/menu.pdf",
                              _client(lambda r: httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})),
                              _public))
    assert out == ("application/pdf", pdf)
    out = run(menu_link.fetch("https://m.example/m.jpg",
                              _client(lambda r: httpx.Response(200, content=b"jpg", headers={"content-type": "image/jpeg"})),
                              _public))
    assert out == ("image/jpeg", b"jpg")


def test_pages_without_prices_and_oversized_pages_are_refused(monkeypatch):
    page = "<html><body><div id=app></div><script>render()</script></body></html>"
    with pytest.raises(ImportError_, match="photos or a PDF"):
        run(menu_link.fetch("https://m.example/", _client(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})), _public))
    monkeypatch.setattr(menu_link, "MAX_BYTES", 100)
    with pytest.raises(ImportError_, match="too large"):
        run(menu_link.fetch("https://m.example/", _client(lambda r: httpx.Response(200, content=b"x" * 500, headers={"content-type": "text/html"})), _public))
    with pytest.raises(ImportError_, match="404"):
        run(menu_link.fetch("https://m.example/", _client(lambda r: httpx.Response(404)), _public))


def test_page_text_keeps_lines_and_drops_scripts():
    text = menu_link.page_text("<h2>Soups</h2><p>Drumstick Soup <b>$6.99</b></p><style>p{}</style>"
                               "<table><tr><td>Tea</td><td>$2.00</td></tr></table><img alt='Paya Soup'>")
    assert text.splitlines() == ["Soups", "Drumstick Soup $6.99", "| Tea | $2.00", "Paya Soup"]
