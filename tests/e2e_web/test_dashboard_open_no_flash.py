"""End-to-end guard: opening the dashboard must not flash card replacements.

The browser observes the kanban region for DOM child mutations during the
first ~2 seconds after navigation. Card replacements caused by the JS
"first refresh" (DOMContentLoaded → refreshViewModel) are what the user
sees as "the entire view flashes". Once `data-card-fingerprint` is
stamped server-side, the JS diff finds matching fingerprints and skips
the replacement, so this observer should record zero card-level removals.
"""

from __future__ import annotations

from playwright.sync_api import Page


PROBE_SCRIPT = r"""
// Combined probe used by the two boot tests: tracks card-level mutations
// under main#mainContent, watches data-booting transitions, and hooks
// fetch so callers can wait on explicit boot signals (no fixed sleeps).
try {
  if (!window.__cardFlash) {
    window.__cardFlash = {
      issueCardsRemoved: 0,
      columnRebuilds: 0,
      ready: false,
      events: [],
      bootingCleared: false,
      firstViewModelFetchEnd: null,
    };
  }
  const probe = window.__cardFlash;
  const t0 = performance.now();
  const log = (kind, detail) => {
    probe.events.push({
      t: Number((performance.now() - t0).toFixed(1)),
      kind,
      detail: detail || {},
    });
  };

  // Wrap fetch to record when /api/view-model* responses come back.
  const origFetch = window.fetch?.bind(window);
  if (origFetch && !window.__fetchHooked) {
    window.__fetchHooked = true;
    window.fetch = async (...args) => {
      const url = String(args[0] || '');
      const isVm = url.includes('/api/view-model');
      if (isVm) log('fetch.start', { url });
      const r = await origFetch(...args);
      if (isVm) {
        log('fetch.end', { url, status: r.status });
        if (probe.firstViewModelFetchEnd === null) {
          probe.firstViewModelFetchEnd = performance.now() - t0;
        }
      }
      return r;
    };
  }

  const installObservers = () => {
    if (!document.documentElement) return false;
    if (probe.ready) return true;
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.attributeName === 'data-booting') {
          const value = document.documentElement.getAttribute('data-booting');
          log('booting', { value });
          if (value === null) probe.bootingCleared = true;
        }
      }
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-booting'],
    });
    const main = document.querySelector('main#mainContent');
    if (!main) return false;
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const removed of m.removedNodes) {
          if (removed?.nodeType !== 1) continue;
          if (removed.classList?.contains('issue-card')) {
            probe.issueCardsRemoved += 1;
          }
          if (removed.classList?.contains('column-cards')) {
            probe.columnRebuilds += 1;
          }
        }
      }
    }).observe(main, { childList: true, subtree: true });
    probe.ready = true;
    return true;
  };
  if (!installObservers()) {
    const iv = setInterval(() => { if (installObservers()) clearInterval(iv); }, 5);
    setTimeout(() => clearInterval(iv), 4000);
  }
} catch (e) { console.warn('[boot-probe] ' + e.message); }
"""


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
    page.add_init_script(PROBE_SCRIPT)
    page.goto(f"{base_url}/?embedded=1&theme=dark", wait_until="domcontentloaded")
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
    page.add_init_script(PROBE_SCRIPT)
    page.goto(f"{base_url}/?embedded=1&theme=dark", wait_until="domcontentloaded")
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


def test_concurrent_view_model_and_snapshot_refresh_do_not_dedup_across_modes(
    page: Page, web_server: dict[str, object]
) -> None:
    """A view-model fetch in flight must NOT be reused for a snapshot
    caller — snapshot needs the row payload that drives refreshIssueRows.
    If the dedup is mode-blind, a list-changing SSE event (queue.changed,
    session.started, ...) silently skips refreshIssueRows.
    """
    base_url = str(web_server["url"])
    page.add_init_script(PROBE_SCRIPT)
    page.goto(f"{base_url}/?embedded=1&theme=dark", wait_until="domcontentloaded")
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
