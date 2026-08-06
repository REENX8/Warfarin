"""Front-end assets must be self-hosted.

A hospital network that blocks external CDNs previously left every page
completely unstyled, and the Tailwind CDN build is documented as
development-only. These tests keep the assets local.
"""
import pathlib
import re

import pytest

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

EXTERNAL_HOSTS = [
    "cdn.tailwindcss.com",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "unpkg.com",
    "cdnjs.cloudflare.com",
]


@pytest.mark.parametrize("template", sorted(TEMPLATE_DIR.glob("*.html")), ids=lambda p: p.name)
def test_templates_do_not_reference_external_hosts(template):
    text = template.read_text(encoding="utf-8")
    for host in EXTERNAL_HOSTS:
        assert host not in text, f"{template.name} still loads assets from {host}"


def test_required_static_files_exist():
    for relative in (
        "css/app.css", "css/public.css", "css/fonts.css", "js/chart.min.js",
        "fonts/sarabun-thai-400.woff2", "fonts/sarabun-latin-400.woff2",
    ):
        assert (STATIC_DIR / relative).is_file(), f"missing static asset: {relative}"


def test_font_css_points_at_local_files():
    css = (STATIC_DIR / "css" / "fonts.css").read_text(encoding="utf-8")
    urls = re.findall(r"url\('([^']+)'\)", css)
    assert urls
    for url in urls:
        assert url.startswith("/static/fonts/"), url
        assert (STATIC_DIR / url.removeprefix("/static/")).is_file(), url


def test_stylesheet_defines_every_component_class():
    """The component classes the templates rely on must all be styled."""
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    public_css = (STATIC_DIR / "css" / "public.css").read_text(encoding="utf-8")
    combined = css + public_css
    for name in (
        "card", "card-header", "stat-card", "btn", "btn-primary", "btn-secondary",
        "input", "label", "badge", "badge-success", "badge-danger", "data-table",
        "nav-link", "flash", "flash-danger", "pager", "field-error",
        "notice", "notice-danger", "pill", "row", "section-title",
    ):
        assert f".{name} " in combined or f".{name}{{" in combined or f".{name}," in combined, name


def test_static_assets_are_served(anon_client):
    for path in (
        "/static/css/app.css", "/static/css/public.css", "/static/css/fonts.css",
        "/static/js/chart.min.js", "/static/fonts/sarabun-thai-400.woff2",
    ):
        response = anon_client.get(path)
        assert response.status_code == 200, path
        assert response.content, path


def test_csp_allows_only_same_origin(anon_client):
    policy = anon_client.get("/login").headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    for host in EXTERNAL_HOSTS:
        assert host not in policy, f"CSP still allows {host}"
    assert "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy


def test_pages_load_the_local_stylesheet(anon_client, admin_client):
    assert "/static/css/app.css" in anon_client.get("/login").text
    assert "/static/css/app.css" in admin_client.get("/dashboard").text
