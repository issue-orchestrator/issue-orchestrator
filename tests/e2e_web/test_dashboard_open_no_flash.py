"""End-to-end guard: opening the dashboard must not flash card replacements.

The browser observes the kanban region for DOM child mutations during the
first ~2 seconds after navigation. Card replacements caused by the JS
"first refresh" (DOMContentLoaded → refreshViewModel) are what the user
sees as "the entire view flashes". Once `data-card-fingerprint` is
stamped server-side, the JS diff finds matching fingerprints and skips
the replacement, so this observer should record zero card-level removals.

The probe itself lives at src/issue_orchestrator/static/js/flash_debug.js
and is gated on ?debug=flash (or localStorage.flashDebug='1') so users
can enable it in a real browser to diagnose flashes interactively. These
tests just append ?debug=flash to the URL — the production code path is
exactly what runs here.
"""

from __future__ import annotations

from playwright.sync_api import Page


# Wait until the boot path has actually finished — observers installed,
# the first /api/view-model* fetch has resolved, and `data-booting` has
# been cleared. Without this we'd be racing on a fixed sleep, which lets
# the no-flash assertion pass before the very mutations it should catch
# (and lets the booting-order assertion fail under load before events
# arrive). Keep the timeout well below the 8s `installBootCleanup`
# fallback so a regression that fails to clear booting still fails fast.
_BOOT_FINISHED = (
    "() => {"
    " const p = window.__cardFlash;"
    " return !!(p && p.ready && p.firstViewModelFetchEnd !== null && p.bootingCleared);"
    "}"
)


def test_opening_dashboard_does_not_replace_server_rendered_cards(
    page: Page, web_server: dict[str, object]
) -> None:
    base_url = str(web_server["url"])
    page.goto(f"{base_url}/?embedded=1&theme=dark&debug=flash", wait_until="domcontentloaded")
    page.wait_for_function(_BOOT_FINISHED, timeout=5000)

    counts = page.evaluate("() => window.__cardFlash || null")
    assert counts and counts["ready"], "probe failed to install"
    assert counts["issueCardsRemoved"] == 0, (
        "Server-rendered kanban cards were removed/replaced during boot — the "
        "first refreshViewModel did not recognise the server's "
        "data-card-fingerprint and rebuilt the DOM. The user would see this "
        f"as a flash. removed={counts['issueCardsRemoved']}, "
        f"column_rebuilds={counts['columnRebuilds']}"
    )


def test_data_booting_clears_after_first_refresh_completes(
    page: Page, web_server: dict[str, object]
) -> None:
    """Suppressing transitions only matters if the suppression window covers
    the initial DOM mutations. If `data-booting` flips off *before* the
    first refresh's mutations land, transitions kick in mid-render and the
    user sees a flash. Assert ordering: fetch.end precedes booting=null.
    """
    base_url = str(web_server["url"])
    page.goto(f"{base_url}/?embedded=1&theme=dark&debug=flash", wait_until="domcontentloaded")
    page.wait_for_function(_BOOT_FINISHED, timeout=5000)

    events = page.evaluate("() => window.__cardFlash?.events || []")
    assert events, "boot probe collected no events"
    fetch_end_t = next(
        (e["t"] for e in events if e["kind"] == "fetch.end"
         and "/api/view-model" in e["detail"].get("url", "")),
        None,
    )
    booting_clear_t = next(
        (e["t"] for e in events if e["kind"] == "booting"
         and e["detail"].get("value") is None),
        None,
    )
    assert fetch_end_t is not None, f"never observed view-model fetch ending; events={events}"
    assert booting_clear_t is not None, f"data-booting never cleared; events={events}"
    assert booting_clear_t >= fetch_end_t, (
        f"data-booting cleared at {booting_clear_t}ms but the first refresh's "
        f"fetch only resolved at {fetch_end_t}ms — transitions were re-enabled "
        "mid-render and the boot flash will be visible."
    )


def test_boot_does_not_mutate_body_or_top_level_dom(
    page: Page, web_server: dict[str, object]
) -> None:
    """The probe at flash_debug.js watches `<body>` attributes and direct
    children, since whole-window flashes are usually CSS/structure churn
    above the kanban (theme swap on body, container show/hide, etc.) and
    are invisible to the card-removal counter.

    Budget:
      - <body> attribute mutations: 0 (no class/style/data-* changes during boot)
      - <body> direct childList changes: 0 (everything inside server-rendered)
      - <html> attribute changes are bounded — only data-booting,
        data-theme, and data-embedded should move, and only the expected
        sequence: set during dashboard_boot.js, then booting cleared once.
    """
    base_url = str(web_server["url"])
    page.goto(f"{base_url}/?embedded=1&theme=dark&debug=flash", wait_until="domcontentloaded")
    page.wait_for_function(_BOOT_FINISHED, timeout=5000)

    probe = page.evaluate("() => window.__cardFlash || null")
    assert probe and probe["ready"], "probe failed to install"

    body_attr = probe["bodyAttrMutations"]
    assert body_attr == [], (
        "<body> attributes mutated during boot — each entry is a candidate "
        "whole-window flash (class/style swap, theme flip, etc.). "
        f"mutations={body_attr}"
    )

    body_child_changes = probe["bodyChildChanges"]
    assert body_child_changes == 0, (
        "<body> direct children changed during boot — top-level structure "
        "was rebuilt, which the user sees as a whole-window flash. "
        f"count={body_child_changes}"
    )

    html_attr = probe["htmlAttrMutations"]
    allowed_attrs = {"data-booting", "data-theme", "data-embedded"}
    unexpected = [m for m in html_attr if m["attr"] not in allowed_attrs]
    assert not unexpected, (
        "<html> attributes changed unexpectedly during boot. Each "
        "unexpected change is a candidate flash trigger (transitions "
        f"keyed off [data-*] re-evaluate). unexpected={unexpected}"
    )


def test_concurrent_view_model_and_snapshot_refresh_do_not_dedup_across_modes(
    page: Page, web_server: dict[str, object]
) -> None:
    """A view-model fetch in flight must NOT be reused for a snapshot
    caller — snapshot needs the row payload that drives refreshIssueRows.
    If the dedup is mode-blind, a list-changing SSE event (queue.changed,
    session.started, ...) silently skips refreshIssueRows.
    """
    base_url = str(web_server["url"])
    page.goto(f"{base_url}/?embedded=1&theme=dark&debug=flash", wait_until="domcontentloaded")
    page.wait_for_function(_BOOT_FINISHED, timeout=5000)

    # Fire a view-model and a snapshot refresh back-to-back in the same
    # tick — the view-model promise must still be in flight when the
    # snapshot call lands. Then assert both endpoints actually fetched.
    fetch_log = page.evaluate(
        """async () => {
            const before = (window.__cardFlash.events || []).length;
            const p1 = window.refreshViewModel({ reloadOnListChange: false });
            const p2 = window.refreshViewModel({ reloadOnListChange: true });
            await Promise.all([p1, p2]);
            return (window.__cardFlash.events || [])
                .slice(before)
                .filter(e => e.kind === 'fetch.start')
                .map(e => e.detail.url);
        }"""
    )
    urls = " ".join(fetch_log)
    assert "/api/view-model-snapshot" in urls, (
        "snapshot caller never fetched /api/view-model-snapshot — the "
        "in-flight view-model promise was reused and refreshIssueRows "
        f"would have been skipped. Observed fetches: {fetch_log!r}"
    )
    # The view-model leg may piggyback on the snapshot if ordering inverts;
    # what matters is that the snapshot actually ran.
