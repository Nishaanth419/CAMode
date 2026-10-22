"""Public pages: app shell meta tags, privacy policy, robots.txt, sitemap.xml, 404s and icons."""

from __future__ import annotations

import struct
from html import escape
from xml.etree import ElementTree

import pytest

from app import plans, privacy, public_pages

ORIGIN = "https://camode.example"


@pytest.fixture
def origin(monkeypatch):
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)
    return ORIGIN


def test_app_shell_has_absolute_open_graph_urls_and_noindex(client, origin):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert f'<meta property="og:image" content="{origin}/og-image.jpg" />' in html
    assert f'<meta property="og:url" content="{origin}/" />' in html
    assert '<meta name="robots" content="noindex" />' in html
    assert public_pages.ORIGIN_PLACEHOLDER not in html
    assert client.get("/index.html").text == html


def test_app_shell_escapes_a_forwarded_host(client):
    response = client.get("/", headers={"x-forwarded-host": 'evil.example"><script>'})

    assert '"><script>' not in response.text
    assert "evil.example&quot;&gt;&lt;script&gt;" in response.text


def test_privacy_page_is_public_and_shows_the_current_notice(client):
    response = client.get("/privacy")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<title>Privacy policy · CA Mode</title>" in html
    assert privacy.POLICY_VERSION in html
    updated = public_pages.policy_updated_on()
    assert f'<time datetime="{updated.isoformat()}">{updated.day} {updated:%B %Y}</time>' in html
    for section in privacy.NOTICE:
        assert escape(section["title"]) in html
        assert escape(section["body"]) in html
    assert 'name="robots"' not in html


def test_notice_says_there_is_no_third_party_tracking():
    text = " ".join(section["body"] for section in privacy.NOTICE)

    assert "no third-party analytics or tracking" in text


def test_policy_date_comes_from_the_version():
    assert public_pages.policy_updated_on().isoformat() == privacy.POLICY_VERSION.split(".")[0]


def test_robots_blocks_api_and_auth_and_points_to_the_sitemap(client, origin):
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    lines = response.text.splitlines()
    assert "Disallow: /api/" in lines
    assert "Disallow: /auth/" in lines
    assert f"Sitemap: {origin}/sitemap.xml" in lines


def test_sitemap_lists_only_the_privacy_and_pricing_pages(client, origin):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ElementTree.fromstring(response.text)
    entries = {
        url.find("s:loc", namespace).text: url.find("s:lastmod", namespace).text
        for url in root.findall("s:url", namespace)
    }
    assert entries == {
        f"{origin}/privacy": public_pages.policy_updated_on().isoformat(),
        f"{origin}/pricing": plans.PRICES_UPDATED.isoformat(),
    }


@pytest.mark.parametrize("path", ["/does-not-exist", "/js/missing.js", "/privacy/extra"])
def test_unknown_page_returns_the_html_404_page(client, path):
    response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text
    assert '<meta name="robots" content="noindex" />' in response.text


@pytest.mark.parametrize("path", ["/api/does-not-exist", "/auth/does-not-exist"])
def test_unknown_api_path_keeps_the_json_404(client, path):
    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_other_http_errors_keep_the_json_body(client):
    response = client.post("/privacy")

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}


def test_not_found_route_responses_from_handlers_are_unchanged(client, signed_in):
    response = client.get("/api/chats/no-such-chat")

    assert response.status_code == 404
    assert response.json() == {"error": "Chat not found."}


@pytest.mark.parametrize(("path", "content_type", "size"), [
    ("/icon-192.png", "image/png", (192, 192)),
    ("/icon-512.png", "image/png", (512, 512)),
    ("/icon-maskable-512.png", "image/png", (512, 512)),
    ("/apple-touch-icon.png", "image/png", (180, 180)),
])
def test_png_icons_are_served_at_their_declared_size(client, path, content_type, size):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", response.content[16:24]) == size


def test_favicon_ico_and_open_graph_image_are_served(client):
    ico = client.get("/favicon.ico")
    og = client.get("/og-image.jpg")

    assert ico.status_code == 200
    assert ico.content[:4] == b"\x00\x00\x01\x00"
    assert og.status_code == 200
    assert og.headers["content-type"] == "image/jpeg"
    assert og.content[:3] == b"\xff\xd8\xff"


def test_manifest_lists_png_and_maskable_icons(client):
    manifest = client.get("/manifest.webmanifest").json()

    icons = {(icon["src"], icon["purpose"]) for icon in manifest["icons"]}
    assert ("/icon-192.png", "any") in icons
    assert ("/icon-512.png", "any") in icons
    assert ("/icon-maskable-512.png", "maskable") in icons
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_shell_template_is_read_once():
    public_pages._shell_template.cache_clear()
    public_pages.app_shell(ORIGIN)
    public_pages.app_shell(ORIGIN)

    assert public_pages._shell_template.cache_info().misses == 1
