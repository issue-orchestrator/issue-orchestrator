"""End-to-end guard: the settings form must post a typed payload.

Regression for "Validation failed: nits_by_agent: Input should be a valid
dictionary": the form rendered dict-typed registry fields through a
catch-all text-input branch and posted the dict's Python repr as a string,
bricking EVERY save (the form posts all fields). The form now renders a
key/value dict editor and encodes the payload via the server-classified
control kinds in static/js/settings_form_controls.js.

These tests drive the real page and intercept the outgoing POST at the
network boundary - the wire payload IS the contract with the strict
pydantic validation (which tests/unit/test_web_publish_settings_static.py
pins on the server side via the GET->POST round-trip).
"""

from __future__ import annotations

import json

from playwright.sync_api import Page


def _open_review_tab(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings", wait_until="domcontentloaded")
    page.get_by_role("tab", name="Review").click()


def test_dict_editor_posts_typed_object(page: Page, web_server: dict[str, object]) -> None:
    base_url = str(web_server["url"])
    _open_review_tab(page, base_url)

    captured: list[dict] = []

    def fulfill_save(route) -> None:  # noqa: ANN001 - playwright route
        captured.append(json.loads(route.request.post_data))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"success": True, "restart_required": False, "warnings": []}),
        )

    page.route("**/api/settings", fulfill_save)

    editor = page.locator('[data-field="nits_by_agent"]')
    editor.get_by_role("button", name="Add Nit Policy By Agent entry").click()
    row = editor.locator(".dict-row").first
    row.locator(".dict-key").fill("agent:frontend")
    row.locator(".dict-value").select_option("address")

    save = page.locator("#saveBtn")
    assert save.is_enabled(), "editing the dict must mark the form dirty"
    save.click()
    page.wait_for_selector(".status-message.success")

    assert len(captured) == 1
    payload = captured[0]
    # The regression: this was the string "{}" / "{'agent:frontend': ...}".
    assert payload["review"]["nits_by_agent"] == {"agent:frontend": "address"}
    # Spot-check the other typed encodings on the same wire payload.
    assert isinstance(payload["concurrency"]["max_concurrent_sessions"], int)
    assert isinstance(payload["review"]["post_publish_checks_pending_timeout_seconds"], (int, float))
    assert payload["filtering"]["label"] is None or isinstance(payload["filtering"]["label"], str)


def test_dict_editor_blocks_save_on_empty_key(page: Page, web_server: dict[str, object]) -> None:
    base_url = str(web_server["url"])
    _open_review_tab(page, base_url)

    posts: list[str] = []

    def reject_save(route) -> None:  # noqa: ANN001 - playwright route
        posts.append(route.request.post_data)
        route.abort()

    page.route("**/api/settings", reject_save)

    editor = page.locator('[data-field="nits_by_agent"]')
    add = editor.get_by_role("button", name="Add Nit Policy By Agent entry")
    # First row carries a real entry (makes the form dirty), second row is
    # left empty - the save must be blocked with an explicit message, not
    # silently drop the row.
    add.click()
    first = editor.locator(".dict-row").nth(0)
    first.locator(".dict-key").fill("agent:frontend")
    add.click()

    page.locator("#saveBtn").click()

    status = page.locator("#statusMessage")
    status.wait_for(state="visible")
    assert "Cannot save" in status.inner_text()
    assert "empty key" in status.inner_text()
    error = editor.locator(".field-error")
    assert error.is_visible()
    assert "empty key" in error.inner_text()
    assert posts == [], "a blocked save must not reach the API"


def test_dict_editor_rows_are_keyboard_reachable(
    page: Page, web_server: dict[str, object]
) -> None:
    """Repo a11y rule: interactive controls must be keyboard reachable and
    expose accessible names."""
    base_url = str(web_server["url"])
    _open_review_tab(page, base_url)

    editor = page.locator('[data-field="nits_by_agent"]')
    editor.get_by_role("button", name="Add Nit Policy By Agent entry").click()

    # Focus lands on the new key input; tab reaches value select then remove.
    focused = page.evaluate("() => document.activeElement.className")
    assert "dict-key" in focused
    page.keyboard.type("agent:frontend")
    page.keyboard.press("Tab")
    assert "dict-value" in page.evaluate("() => document.activeElement.className")
    page.keyboard.press("Tab")
    assert "dict-remove" in page.evaluate("() => document.activeElement.className")

    remove = editor.locator(".dict-remove").first
    assert remove.get_attribute("aria-label") == "Remove Nit Policy By Agent entry agent:frontend"
    page.keyboard.press("Enter")
    assert editor.locator(".dict-row").count() == 0
