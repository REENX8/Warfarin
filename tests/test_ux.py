"""UX affordances: assets, accessibility markup and keyboard navigation."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"


# --- assets -----------------------------------------------------------------
def test_ux_scripts_exist_and_are_served(anon_client):
    for path in ("/static/js/app.js", "/static/js/patient.js"):
        response = anon_client.get(path)
        assert response.status_code == 200, path
        assert response.content


def test_staff_pages_load_the_shared_script(admin_client):
    assert "/static/js/app.js" in admin_client.get("/dashboard").text


def test_patient_pages_load_the_accessibility_script(anon_client, patient):
    assert "/static/js/patient.js" in anon_client.get(f"/p/{patient['access_token']}").text


# --- accessibility ----------------------------------------------------------
def test_staff_pages_have_a_skip_link_and_main_landmark(admin_client):
    text = admin_client.get("/dashboard").text
    assert 'class="skip-link"' in text
    assert 'id="mainContent"' in text
    assert 'href="#mainContent"' in text


def test_navigation_is_labelled(admin_client):
    assert 'aria-label="เมนูหลัก"' in admin_client.get("/dashboard").text


def test_patient_pages_expose_the_display_controls(anon_client, patient, dose_token):
    for path in (f"/p/{patient['access_token']}", f"/dose/{dose_token['token_id']}"):
        text = anon_client.get(path).text
        assert 'id="a11yControls"' in text, path
        assert 'aria-label="ปรับการแสดงผล"' in text, path


def test_patient_pages_have_a_main_landmark(anon_client, patient):
    assert '<main id="mainContent">' in anon_client.get(f"/p/{patient['access_token']}").text


def test_icon_only_buttons_have_accessible_names(admin_client):
    """Every button whose content is just an SVG needs an aria-label."""
    text = admin_client.get("/dashboard").text
    for match in re.finditer(r"<button\b[^>]*>(.*?)</button>", text, re.DOTALL):
        attributes, body = match.group(0), match.group(1)
        stripped = re.sub(r"<svg.*?</svg>", "", body, flags=re.DOTALL).strip()
        if stripped:
            continue        # the button has visible text
        assert "aria-label" in attributes, f"icon-only button without a label: {attributes[:120]}"


def test_flash_region_is_announced(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/inventory",
        data={"pill_inventory": "12"},
        follow_redirects=True,
    )
    assert 'id="flashMessage"' in response.text
    assert 'role="status"' in response.text


# --- command palette --------------------------------------------------------
def test_search_trigger_is_present_with_shortcut_hint(admin_client):
    text = admin_client.get("/dashboard").text
    assert "data-open-palette" in text
    assert "⌘K" in text


def test_lookup_powers_the_palette(admin_client, patient):
    results = admin_client.get(f"/api/lookup?q={patient['full_name'][:4]}").json()
    assert any(row["patient_id"] == patient["patient_id"] for row in results)
    assert {"patient_id", "full_name", "hn", "active"} <= set(results[0])


def test_palette_actions_point_at_real_routes(admin_client):
    """Every hard-coded destination in the palette must resolve."""
    script = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    block = script.split("var PALETTE_ACTIONS = [", 1)[1].split("];", 1)[0]
    urls = re.findall(r"url:\s*'([^']+)'", block)
    assert urls
    for url in urls:
        assert admin_client.get(url).status_code == 200, url


# --- research navigation ----------------------------------------------------
def test_research_appears_in_the_navigation(admin_client):
    text = admin_client.get("/dashboard").text
    assert 'href="/research"' in text
    assert "งานวิจัย" in text


def test_patient_page_links_to_enrolment(admin_client, patient):
    text = admin_client.get(f"/patients/{patient['patient_id']}").text
    assert f"/research/enroll/{patient['patient_id']}" in text


# --- elderly-friendly styling ----------------------------------------------
def test_public_stylesheet_defines_text_scaling_and_contrast():
    css = (STATIC / "css" / "public.css").read_text(encoding="utf-8")
    assert "--text-scale" in css
    assert ".high-contrast" in css
    assert ".a11y-button" in css


def test_tap_targets_meet_a_minimum_size():
    """Buttons and inputs on patient pages need a finger-sized hit area."""
    css = (STATIC / "css" / "public.css").read_text(encoding="utf-8")
    assert "min-height: 46px" in css     # form fields
    assert "min-height: 40px" in css     # accessibility buttons
    assert "min-width: 22px" in css      # checkboxes and radios


def test_reduced_motion_is_respected():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css


def test_focus_styles_are_defined():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert ":focus-visible" in css


def test_toast_and_palette_styles_exist():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".toast", ".toast-host", ".palette", ".palette-item", ".shortcut-help"):
        assert selector in css, selector


def test_ux_chrome_is_hidden_when_printing():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    print_block = css.split("@media print", 1)[-1]
    for selector in (".toast-host", ".palette", ".skip-link"):
        assert selector in print_block, selector


# --- double-submit protection ----------------------------------------------
def test_dose_confirm_form_is_marked_for_guarding():
    template = (TEMPLATES / "dose_confirm.html").read_text(encoding="utf-8")
    assert "data-confirm-dose" in template


def test_app_script_guards_post_forms():
    script = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    assert "guardForms" in script
    assert "button.disabled = true" in script


def test_patient_script_persists_preferences():
    script = (STATIC / "js" / "patient.js").read_text(encoding="utf-8")
    assert "localStorage" in script
    assert "warfarin.textScale" in script
    assert "warfarin.highContrast" in script
    # Storage can throw in private mode; the page must still work.
    assert "catch (error)" in script


# --- high-contrast coverage -------------------------------------------------
PATIENT_TEMPLATES = [
    "portal.html", "dose_confirm.html", "dose_result.html",
    "symptom_form.html", "symptom_result.html", "_public_base.html",
]


@pytest.mark.parametrize("name", PATIENT_TEMPLATES)
def test_patient_templates_have_no_muted_inline_colours(name):
    """Inline colours beat the stylesheet, so high-contrast mode could not
    override them. Muted text must use the .muted class instead."""
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    muted = {"#94A3B8", "#8A9BAA", "#64748B", "#7E97AF"}
    for colour in muted:
        assert f"color:{colour}" not in text.replace(" ", ""), \
            f"{name} still sets {colour} inline; use class=\"muted\""


def test_high_contrast_overrides_muted_text():
    css = (STATIC / "css" / "public.css").read_text(encoding="utf-8")
    assert ".muted {" in css
    assert ".high-contrast .muted" in css
    assert ".high-contrast .pill-default" in css


def test_high_contrast_renders_on_patient_pages(anon_client, patient):
    text = anon_client.get(f"/p/{patient['access_token']}").text
    assert 'class="muted"' in text
