"""Pages served without a profile: the app shell, privacy policy, 404 page, robots.txt and sitemap.xml.

The privacy page renders ``privacy.NOTICE``, so it always matches what profiles accept in the
app. Only the privacy page is listed for search engines: the app shell shows the names of the
profiles on this computer, so it carries ``noindex`` (crawlers are still allowed to fetch it, or
they could not read that tag). ``/api/`` and ``/auth/`` are disallowed outright.
"""

from __future__ import annotations

import functools
from datetime import date
from html import escape
from pathlib import Path
from typing import Final
from xml.sax.saxutils import escape as xml_escape

from . import privacy

PUBLIC_DIR: Final = Path(__file__).resolve().parent.parent / "frontend"
PRIVACY_PATH: Final = "/privacy"
INDEXABLE_PATHS: Final = (PRIVACY_PATH,)
ORIGIN_PLACEHOLDER: Final = "{{origin}}"
# Paths whose 404s stay JSON for API clients; everything else gets the HTML page.
API_PREFIXES: Final = ("/api/", "/auth/")


def policy_updated_on() -> date:
    """Return the date in ``POLICY_VERSION`` (``YYYY-MM-DD.n``)."""
    return date.fromisoformat(privacy.POLICY_VERSION.split(".")[0])


@functools.cache
def _shell_template() -> str:
    return (PUBLIC_DIR / "index.html").read_text(encoding="utf-8")


def app_shell(origin: str) -> str:
    """Return index.html with absolute URLs (Open Graph needs them) under ``origin``."""
    return _shell_template().replace(ORIGIN_PLACEHOLDER, escape(origin, quote=True))


def robots_txt(origin: str) -> str:
    return (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {origin}/sitemap.xml\n"
    )


def sitemap_xml(origin: str) -> str:
    lastmod = policy_updated_on().isoformat()
    urls = "".join(
        f"  <url><loc>{xml_escape(origin + path)}</loc><lastmod>{lastmod}</lastmod></url>\n"
        for path in INDEXABLE_PATHS
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}"
        "</urlset>\n"
    )


def is_api_path(path: str) -> bool:
    return path.startswith(API_PREFIXES)


def _page(*, title: str, description: str, body: str, is_indexable: bool) -> str:
    robots = "" if is_indexable else '  <meta name="robots" content="noindex" />\n'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="theme-color" content="#f4f7f5" media="(prefers-color-scheme: light)" />
  <meta name="theme-color" content="#0a1a15" media="(prefers-color-scheme: dark)" />
  <meta name="description" content="{escape(description)}" />
{robots}  <title>{escape(title)} · CA Mode</title>
  <link rel="icon" href="/favicon.ico" sizes="48x48" />
  <link rel="icon" type="image/svg+xml" href="/icon.svg" />
  <link rel="apple-touch-icon" href="/apple-touch-icon.png" />
  <link rel="stylesheet" href="/css/public.css?v=1" />
</head>
<body>
  <main class="public-page">
    <a class="public-brand" href="/"><img src="/icon.svg" width="36" height="36" alt="" />CA Mode</a>
{body}
  </main>
</body>
</html>
"""


def privacy_page() -> str:
    """Return the privacy policy page, built from the notice every profile accepts."""
    updated = policy_updated_on()
    sections = "".join(
        f'    <section>\n      <h2>{escape(item["title"])}</h2>\n      <p>{escape(item["body"])}</p>\n    </section>\n'
        for item in privacy.NOTICE
    )
    body = f"""    <h1>Privacy policy</h1>
    <p class="public-meta">Last updated <time datetime="{updated.isoformat()}">{updated.day} {updated:%B %Y}</time>
      · Notice version {escape(privacy.POLICY_VERSION)}</p>
    <p class="public-lead">CA Mode runs on the computer it is installed on. This is the same notice each profile
      reads and accepts before it can chat, connect INDmoney or save history.</p>
{sections}    <p><a class="public-button" href="/">Open CA Mode</a></p>"""
    return _page(
        title="Privacy policy",
        description="What CA Mode saves on your computer, what it sends to the AI model you choose, and how to export or delete your data.",
        body=body,
        is_indexable=True,
    )


def not_found_page() -> str:
    body = """    <p class="public-code" aria-hidden="true">404</p>
    <h1>Page not found</h1>
    <p class="public-lead">This address is not part of CA Mode. It may have been mistyped, or the page has moved.</p>
    <p><a class="public-button" href="/">Go to CA Mode</a> <a class="public-link" href="/privacy">Privacy policy</a></p>"""
    return _page(
        title="Page not found",
        description="This address is not part of CA Mode.",
        body=body,
        is_indexable=False,
    )
