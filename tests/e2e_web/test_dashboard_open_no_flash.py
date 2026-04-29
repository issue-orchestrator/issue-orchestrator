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
try {
  if (!window.__cardFlash) {
    window.__cardFlash = { issueCardsRemoved: 0, columnRebuilds: 0, ready: false };
  }
  const install = () => {
    if (!document.body) return false;
    const main = document.querySelector('main#mainContent');
    if (!main) return false;
    new MutationObserver((muts) => {
      for (const m of muts) {
        for (const removed of m.removedNodes) {
          if (removed?.nodeType === 1) {
            if (removed.classList?.contains('issue-card')) {
              window.__cardFlash.issueCardsRemoved += 1;
            }
            if (removed.classList?.contains('column-cards')) {
              window.__cardFlash.columnRebuilds += 1;
            }
          }
        }
      }
    }).observe(main, { childList: true, subtree: true });
    window.__cardFlash.ready = true;
    return true;
  };
  if (!install()) {
    const iv = setInterval(() => { if (install()) clearInterval(iv); }, 5);
    setTimeout(() => clearInterval(iv), 2000);
  }
} catch (e) { console.warn('[card-flash-probe] ' + e.message); }
"""


def test_opening_dashboard_does_not_replace_server_rendered_cards(
    page: Page, web_server: dict[str, object]
) -> None:
    base_url = str(web_server["url"])
    page.add_init_script(PROBE_SCRIPT)
    page.goto(f"{base_url}/?embedded=1&theme=dark", wait_until="domcontentloaded")
    # Give the boot path room to run: DOMContentLoaded → first refreshViewModel
    # → SSE onopen (deduped via in-flight promise) → mutations settle.
    page.wait_for_timeout(2000)

    counts = page.evaluate("() => window.__cardFlash || null")
    assert counts and counts["ready"], "probe failed to install"
    assert counts["issueCardsRemoved"] == 0, (
        "Server-rendered kanban cards were removed/replaced during boot — the "
        "first refreshViewModel did not recognise the server's "
        "data-card-fingerprint and rebuilt the DOM. The user would see this "
        f"as a flash. removed={counts['issueCardsRemoved']}, "
        f"column_rebuilds={counts['columnRebuilds']}"
    )


BOOT_ATTR_PROBE = r"""
try {
  window.__bootProbe = { events: [] };
  const t0 = performance.now();
  const log = (kind, detail) => {
    window.__bootProbe.events.push({
      t: Number((performance.now() - t0).toFixed(1)),
      kind,
      detail: detail || {},
    });
  };
  // Hook fetch so we can witness when refreshViewModel's network call returns.
  const origFetch = window.fetch?.bind(window);
  if (origFetch) {
    window.fetch = async (...args) => {
      const url = String(args[0] || '');
      if (url.includes('/api/view-model')) log('fetch.start', { url });
      const r = await origFetch(...args);
      if (url.includes('/api/view-model')) log('fetch.end', { url, status: r.status });
      return r;
    };
  }
  const install = () => {
    if (!document.documentElement) return false;
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.attributeName === 'data-booting') {
          log('booting', { value: document.documentElement.getAttribute('data-booting') });
        }
      }
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-booting'] });
    return true;
  };
  if (!install()) {
    const iv = setInterval(() => { if (install()) clearInterval(iv); }, 5);
    setTimeout(() => clearInterval(iv), 2000);
  }
} catch (e) { console.warn('[boot-attr-probe] ' + e.message); }
"""


def test_data_booting_clears_after_first_refresh_completes(
    page: Page, web_server: dict[str, object]
) -> None:
    """Suppressing transitions only matters if the suppression window covers
    the initial DOM mutations. If `data-booting` flips off *before* the
    first refresh's mutations land, transitions kick in mid-render and the
    user sees a flash. Assert ordering: fetch.end precedes booting=null.
    """
    base_url = str(web_server["url"])
    page.add_init_script(BOOT_ATTR_PROBE)
    page.goto(f"{base_url}/?embedded=1&theme=dark", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    events = page.evaluate("() => window.__bootProbe?.events || []")
    assert events, "boot probe collected no events"
    # data-booting starts as 'true' (set by inline boot script before our probe
    # observes it), then must clear to null after the first /api/view-model
    # fetch resolves.
    fetch_end_t = next(
        (e["t"] for e in events if e["kind"] == "fetch.end" and "/api/view-model" in e["detail"].get("url", "")),
        None,
    )
    booting_clear_t = next(
        (e["t"] for e in events if e["kind"] == "booting" and e["detail"].get("value") is None),
        None,
    )
    assert fetch_end_t is not None, f"never observed view-model fetch ending; events={events}"
    assert booting_clear_t is not None, f"data-booting never cleared; events={events}"
    assert booting_clear_t >= fetch_end_t, (
        f"data-booting cleared at {booting_clear_t}ms but the first refresh's "
        f"fetch only resolved at {fetch_end_t}ms — transitions were re-enabled "
        "mid-render and the boot flash will be visible."
    )
