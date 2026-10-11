"""Browser guardrail: Control Center error toasts stay reachable (#8222).

An engine command failure now reaches the operator as a sticky error toast
carrying the cause. On a short viewport, several long causes must not push
the newest notification or its dismiss control off-screen, and each cause
must stay keyboard-scrollable.
"""

from pathlib import Path

from playwright.sync_api import Page, expect

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "issue_orchestrator" / "static"


def test_long_error_toasts_keep_the_newest_and_its_dismiss_in_view(page: Page) -> None:
    page.set_viewport_size({"width": 320, "height": 568})
    page.set_content(
        '<div class="toast-container" id="toastContainer" role="status" aria-live="polite"></div>'
    )
    page.add_style_tag(path=str(STATIC / "css" / "control_center.css"))
    page.add_script_tag(path=str(STATIC / "js" / "control_center.js"))

    page.evaluate(
        """
        () => {
            for (let i = 1; i <= 5; i += 1) {
                showToast(`Failed to pause repository engine: cause ${i} ` + 'x'.repeat(2000), 'error');
            }
        }
        """
    )
    page.wait_for_timeout(4500)  # past the old auto-dismiss

    toasts = page.locator("#toastContainer .toast")
    expect(toasts).to_have_count(5)
    newest = toasts.first
    expect(newest.locator(".toast-message")).to_contain_text("cause 5")
    expect(newest.locator(".toast-close")).to_be_in_viewport()

    # The long cause scrolls inside its own bounded, focusable region.
    message = newest.locator(".toast-message")
    message.focus()
    expect(message).to_be_focused()
    bounds = message.evaluate(
        "el => ({client: el.clientHeight, scroll: el.scrollHeight, viewport: window.innerHeight})"
    )
    assert bounds["scroll"] > bounds["client"]
    assert bounds["client"] <= bounds["viewport"] * 0.4 + 1
    page.keyboard.press("PageDown")  # keyboard scrolling may animate
    page.wait_for_function(
        "() => document.querySelector('#toastContainer .toast .toast-message').scrollTop > 0",
        timeout=3000,
    )

    # Older toasts stay reachable: tabbing to the oldest dismiss scrolls it into view.
    oldest_close = toasts.last.locator(".toast-close")
    oldest_close.focus()
    expect(oldest_close).to_be_in_viewport()
    oldest_close.press("Enter")
    expect(toasts).to_have_count(4)
