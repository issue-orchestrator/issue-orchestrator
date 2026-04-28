"""Browser round-trip verifying the Control Center "Back to repositories"
affordance survives Dashboard → Settings → Dashboard.

The Dashboard decides whether to show #embeddedBack by reading `embedded=1`
from its URL at load time. The CC loads the iframe with `embedded=1&theme=`
so that both flags must be propagated forward when the Dashboard navigates
to /settings and when Settings navigates back. If either hop drops the
embedded context, the round-trip reload of the Dashboard hides the
back-to-repositories button and the iframe becomes a dead end.

This test exercises the real shared helper (static/js/embedded_nav.js) in
a real browser against the real templates so it would fail if the wiring
ever regressed — the string-level guardrails alone cannot see this.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, expect


def _goto(page: Page, base_url: str, path: str) -> None:
    page.goto(f"{base_url}{path}", wait_until="domcontentloaded")


def test_embedded_back_button_survives_settings_round_trip(
    page: Page, web_server: dict[str, object]
) -> None:
    base_url = str(web_server["url"])

    # 1. Dashboard loaded embedded with a theme, like CC does.
    _goto(page, base_url, "/?embedded=1&theme=dark")
    embedded_back = page.locator("#embeddedBack")
    expect(embedded_back).to_be_visible()
    expect(embedded_back).to_have_attribute("aria-label", "Back to repositories")

    # 2. Open the dashboard actions menu and click Settings. Use a direct
    #    call to goToSettings() to avoid menu-animation flakiness while still
    #    exercising the helper that wires the menu click.
    page.evaluate("goToSettings()")
    page.wait_for_url("**/settings?**")
    settings_url = urlparse(page.url)
    settings_params = parse_qs(settings_url.query)
    assert settings_params.get("embedded") == ["1"], settings_url.query
    assert settings_params.get("theme") == ["dark"], settings_url.query

    # 3. Click Cancel to return to the Dashboard the same way a user would.
    page.locator("#cancelSettingsBtn").click()
    page.wait_for_url(lambda url: urlparse(url).path == "/" and "embedded=1" in url)
    dashboard_url = urlparse(page.url)
    dashboard_params = parse_qs(dashboard_url.query)
    assert dashboard_params.get("embedded") == ["1"], dashboard_url.query
    assert dashboard_params.get("theme") == ["dark"], dashboard_url.query

    # 4. And the affordance is back — this is the bug the PR fixes.
    expect(page.locator("#embeddedBack")).to_be_visible()
    expect(page.locator("#embeddedBack")).to_have_attribute(
        "aria-label", "Back to repositories"
    )


def test_back_link_from_settings_also_preserves_embedded_context(
    page: Page, web_server: dict[str, object]
) -> None:
    base_url = str(web_server["url"])

    _goto(page, base_url, "/settings?embedded=1&theme=light")

    # The inline script patches the back-link href on DOMContentLoaded.
    back_link = page.locator("#backToDashboardLink")
    expect(back_link).to_have_attribute("href", "/?embedded=1&theme=light")

    back_link.click()
    page.wait_for_url(lambda url: urlparse(url).path == "/" and "embedded=1" in url)
    params = parse_qs(urlparse(page.url).query)
    assert params.get("embedded") == ["1"]
    assert params.get("theme") == ["light"]

    expect(page.locator("#embeddedBack")).to_be_visible()


def test_non_embedded_dashboard_does_not_show_back_affordance(
    page: Page, web_server: dict[str, object]
) -> None:
    # Guardrail on the other direction: loading the Dashboard standalone
    # (no embedded flag) must NOT show the back-to-repositories button,
    # so the helper isn't accidentally forcing it on in every context.
    base_url = str(web_server["url"])
    _goto(page, base_url, "/")
    expect(page.locator("#embeddedBack")).to_be_hidden()


def test_settings_applies_url_theme_on_direct_load(
    page: Page, web_server: dict[str, object]
) -> None:
    """Settings must apply ?theme=<value> on load, matching the Dashboard's
    precedence (url > stored > system). Without this, CC-supplied theme is
    preserved through navigation but the Settings surface still renders in
    the user's local theme, leaving a visible inconsistency inside the
    iframe."""
    base_url = str(web_server["url"])

    # Ensure localStorage is not pre-populated so the URL is load-bearing.
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate("localStorage.removeItem('theme')")

    _goto(page, base_url, "/settings?embedded=1&theme=dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    _goto(page, base_url, "/settings?embedded=1&theme=light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_settings_theme_survives_round_trip_from_dashboard(
    page: Page, web_server: dict[str, object]
) -> None:
    """Load CC-style iframe URL, go to Settings, and confirm Settings
    actually renders in the CC-supplied theme — not just that the URL
    still carries it."""
    base_url = str(web_server["url"])

    # Clear any prior stored theme so precedence is URL-driven.
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate("localStorage.removeItem('theme')")

    _goto(page, base_url, "/?embedded=1&theme=dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    page.evaluate("goToSettings()")
    page.wait_for_url("**/settings?**")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_dashboard_prepaint_theme_uses_stored_light_preference(
    page: Page, web_server: dict[str, object]
) -> None:
    base_url = str(web_server["url"])
    page.emulate_media(color_scheme="dark")

    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate("localStorage.setItem('theme', 'light')")

    _goto(page, base_url, "/")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    background_image = page.locator("body").evaluate(
        "el => getComputedStyle(el).backgroundImage"
    )
    assert "rgb(220, 233, 248)" in background_image
    assert "rgb(15, 23, 34)" not in background_image


def test_embedded_dashboard_uses_embedded_chrome_before_bundle_waits(
    page: Page, web_server: dict[str, object]
) -> None:
    base_url = str(web_server["url"])

    _goto(page, base_url, "/?embedded=1&theme=light")

    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    expect(page.locator("html")).to_have_attribute("data-embedded", "true")
    expect(page.locator("body > .container > header")).to_be_hidden()
    expect(page.locator("#embeddedBack")).to_be_visible()
