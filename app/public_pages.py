"""Pages served without a profile: the app shell, privacy policy, pricing, admin, 404 page, robots.txt and sitemap.xml.

The privacy page renders ``privacy.NOTICE``, so it always matches what profiles accept in the
app, and the pricing page renders ``plans.PLANS``. Only those two are listed for search engines: the app shell shows the names of the
profiles on this computer, so it carries ``noindex`` (crawlers are still allowed to fetch it, or
they could not read that tag). ``/api/``, ``/auth/`` and ``/admin`` are disallowed outright.
"""

from __future__ import annotations

import functools
from datetime import date
from html import escape
from pathlib import Path
from typing import Final
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from . import plans, privacy

PUBLIC_DIR: Final = Path(__file__).resolve().parent.parent / "frontend"
PRIVACY_PATH: Final = "/privacy"
PRICING_PATH: Final = "/pricing"
INDEXABLE_PATHS: Final = (PRIVACY_PATH, PRICING_PATH)
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
        "Disallow: /admin\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {origin}/sitemap.xml\n"
    )


def _last_modified(path: str) -> date:
    return plans.PRICES_UPDATED if path == PRICING_PATH else policy_updated_on()


def sitemap_xml(origin: str) -> str:
    urls = "".join(
        f"  <url><loc>{xml_escape(origin + path)}</loc><lastmod>{_last_modified(path).isoformat()}</lastmod></url>\n"
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


def _page(
    *, title: str, description: str, body: str, is_indexable: bool, is_wide: bool = False, head: str = "",
) -> str:
    page_class = "public-page public-page-wide" if is_wide else "public-page"
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
  <link rel="stylesheet" href="/css/public.css?v=2" />
{head}</head>
<body>
  <main class="{page_class}">
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
{sections}    <p><a class="public-button" href="/">Open CA Mode</a> <a class="public-link" href="{PRICING_PATH}">Pricing</a></p>"""
    return _page(
        title="Privacy policy",
        description="What CA Mode saves on your computer, what it sends to the AI model you choose, and how to export or delete your data.",
        body=body,
        is_indexable=True,
    )


def admin_page() -> str:
    """Return the operator's admin page; ``js/admin.js`` fills it from ``/api/admin/*``."""
    body = """    <h1>Admin</h1>
    <p class="public-lead">Profiles on this copy of CA Mode, their plans and usage. PIN-locked history is never shown here.</p>
    <div id="admin" aria-live="polite"><p class="admin-muted">Loading…</p></div>"""
    return _page(
        title="Admin",
        description="Manage CA Mode profiles and plans.",
        body=body,
        is_indexable=False,
        is_wide=True,
        head='  <link rel="stylesheet" href="/css/admin.css?v=1" />\n  <script src="/js/admin.js?v=1" defer></script>\n',
    )


def not_found_page() -> str:
    body = """    <p class="public-code" aria-hidden="true">404</p>
    <h1>Page not found</h1>
    <p class="public-lead">This address is not part of CA Mode. It may have been mistyped, or the page has moved.</p>
    <p><a class="public-button" href="/">Go to CA Mode</a> <a class="public-link" href="/pricing">Pricing</a>
      <a class="public-link" href="/privacy">Privacy policy</a></p>"""
    return _page(
        title="Page not found",
        description="This address is not part of CA Mode.",
        body=body,
        is_indexable=False,
    )


def _plan_action(plan: plans.Plan, contact: str | None) -> str:
    if plan.id == "free":
        return '<a class="public-button" href="/">Start free</a>'
    if contact is None:
        return '<p class="plan-contact">Ask whoever runs this copy of CA Mode to change your plan.</p>'
    subject = quote(f"CA Mode {plan.name} plan")
    label = "Choose Individual" if plan.id == "individual" else "Contact us"
    return f'<a class="public-button" href="mailto:{escape(contact, quote=True)}?subject={subject}">{label}</a>'


def pricing_page() -> str:
    """Return the pricing page: the plans in ``plans.PLANS`` and why a hosted team copy costs more."""
    free_questions = plans.free_daily_questions()
    zone = plans.reset_zone()
    contact = plans.contact_email()
    individual_credits = f"{plans.PLANS['individual'].monthly_credits or 0:,}"
    cards = ""
    for plan in plans.PLANS.values():
        features = "".join(
            f"<li>{escape(feature.format(free_questions=free_questions, zone=zone, monthly_credits=f'{plan.monthly_credits or 0:,}'))}</li>"
            for feature in plan.features
        )
        period = f' <span class="plan-period">{escape(plan.period)}</span>' if plan.period else ""
        featured = " plan-featured" if plan.id == "individual" else ""
        cards += f"""      <section class="plan{featured}" aria-labelledby="plan-{plan.id}">
        <h2 id="plan-{plan.id}">{escape(plan.name)}</h2>
        <p class="plan-price">{escape(plan.price)}{period}</p>
        <p>{escape(plan.summary)}</p>
        <ul>{features}</ul>
        {_plan_action(plan, contact)}
      </section>
"""
    body = f"""    <h1>Pricing</h1>
    <p class="public-lead">Start free with {free_questions} questions a day. Pay for monthly credits on a privately
      hosted model, or for a copy of CA Mode hosted for your team.</p>
    <div class="plans">
{cards}    </div>
    <section>
      <h2>Why a team costs more than one person</h2>
      <p>Individual plans share one copy of CA Mode whose answers come from a privately hosted language model, and each
        month comes with its own credits. A team or organisation gets its own hosted copy: a server, its
        own database and a model sized for more people, which we set up, back up and keep running. That hosting is what
        raises the price, so it depends on how many members you have and how much they ask.</p>
    </section>
    <section>
      <h2>What counts as a question</h2>
      <p>Each chat message that gets an answer. A question that gets no answer (the model failed, or INDmoney needs
        reconnecting) is not counted. The free count starts again at midnight {escape(zone)}.</p>
    </section>
    <section>
      <h2>Credits and top-ups</h2>
      <p>The Individual plan comes with {individual_credits} credits a month. One credit is 1,000 tokens of the
        model's work (the question, the data it reads and the answer), so a typical answer costs a few credits; a
        question that gets no answer costs nothing. Credits renew on the day of the month your plan started and
        don't carry over. Once they are used you can top up at any time; top-up credits are kept until used.</p>
    </section>
    <section>
      <h2>How to upgrade</h2>
      <p>There is no online checkout yet. Get in touch and we move your existing profile to the new plan, so your chats,
        goals and snapshots stay where they are. Prices are in Indian rupees and exclude GST.</p>
    </section>
    <section>
      <h2>Refund policy</h2>
      <ul>
        <li>Ask within 30 days of paying and we refund the full amount, whatever you have used.</li>
        <li>One refund per user.</li>
        <li>To ask, contact whoever runs this copy of CA Mode.</li>
      </ul>
    </section>
    <p><a class="public-button" href="/">Open CA Mode</a> <a class="public-link" href="{PRIVACY_PATH}">Privacy policy</a></p>"""
    return _page(
        title="Pricing",
        description=(
            f"CA Mode is free for {free_questions} questions a day. Individual plans come with monthly credits on a "
            "privately hosted model and a 30-day refund; teams and organisations get their own hosted copy."
        ),
        body=body,
        is_indexable=True,
        is_wide=True,
    )
