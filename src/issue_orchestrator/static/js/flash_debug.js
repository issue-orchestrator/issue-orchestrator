// Dashboard flash diagnostic probe.
//
// Toggle on with ?debug=flash in the URL or localStorage.flashDebug='1'.
// When enabled, instruments the boot path so the cause of any "window
// flashes on open" is visible in DevTools without rebuilding the app.
//
// Exposes window.__cardFlash with:
//   issueCardsRemoved   count of .issue-card removals under main#mainContent
//   columnRebuilds      count of .column-cards removals (full column rebuilds)
//   htmlAttrMutations   array of {t, attr, oldValue, newValue} for <html> attrs
//                       (catches theme swap, data-booting, mode flips)
//   bodyAttrMutations   array of {t, attr, oldValue, newValue} for <body> attrs
//                       (catches body class/style swaps)
//   bodyChildChanges    count of direct childList mutations on <body>
//                       (catches macro-structure rebuilds)
//   longAnimationFrames array of {t, duration} for frames > 50ms
//                       (each long frame is a candidate "visible flash")
//   stylesheetLoads     array of {t, href} for late <link rel=stylesheet> loads
//   bootingCleared      true once data-booting attribute removed
//   firstViewModelFetchEnd   ms (since probe start) of first /api/view-model* response
//   events              timestamped log: fetch.start, fetch.end, booting, mutation, frame
//
// On bootingCleared, prints a console.group summary so the timeline is
// the first thing you see in DevTools.
//
// The e2e test at tests/e2e_web/test_dashboard_open_no_flash.py drives the
// same probe via ?debug=flash to keep the diagnostic and the regression
// guard in lockstep.

(function () {
    function isEnabled(root) {
        try {
            const params = new URLSearchParams(root.location?.search || '');
            if (params.get('debug') === 'flash') return true;
        } catch (_error) { /* ignore */ }
        try {
            if (root.localStorage?.getItem('flashDebug') === '1') return true;
        } catch (_error) { /* ignore */ }
        return false;
    }

    const root = typeof window !== 'undefined' ? window : null;
    if (!root || !isEnabled(root)) return;

    if (root.__cardFlash) return;
    const probe = {
        issueCardsAdded: 0,
        issueCardsRemoved: 0,
        cardAttrMutations: 0,
        columnRebuilds: 0,
        columnInserts: 0,
        mainAttrMutations: [],
        htmlAttrMutations: [],
        // bodyAttrMutations / bodyChildChanges = direct children of <body>
        // only. These are the canonical "whole-window flash" signals (a
        // top-level structure swap or a class flip on body itself) and the
        // e2e regression test asserts they stay at zero. Subtree mutations
        // (header, banner, status badges) live in the *Subtree counters
        // below — they're useful for live debugging but legitimately
        // non-zero during init even on a clean boot.
        bodyAttrMutations: [],
        bodyChildChanges: 0,
        bodyAttrSubtreeMutations: [],
        bodyChildSubtreeChanges: 0,
        longAnimationFrames: [],
        stylesheetLoads: [],
        ready: false,
        events: [],
        bootingCleared: false,
        firstViewModelFetchEnd: null,
    };
    root.__cardFlash = probe;

    const describeTarget = (el) => {
        if (!el || el.nodeType !== 1) return '?';
        const tag = el.tagName?.toLowerCase() || '?';
        const id = el.id ? '#' + el.id : '';
        const cls = el.classList?.length
            ? '.' + Array.from(el.classList).slice(0, 3).join('.')
            : '';
        return tag + id + cls;
    };

    const t0 = (root.performance || Date).now();
    const now = () => (root.performance || Date).now();
    const log = (kind, detail) => {
        probe.events.push({
            t: Number((now() - t0).toFixed(1)),
            kind,
            detail: detail || {},
        });
    };

    const origFetch = root.fetch ? root.fetch.bind(root) : null;
    if (origFetch && !root.__fetchHooked) {
        root.__fetchHooked = true;
        root.fetch = async (...args) => {
            const url = String(args[0] || '');
            const isApi = url.includes('/api/');
            const isVm = url.includes('/api/view-model');
            if (isApi) log('fetch.start', { url });
            const response = await origFetch(...args);
            if (isApi) {
                log('fetch.end', { url, status: response.status });
                if (isVm && probe.firstViewModelFetchEnd === null) {
                    probe.firstViewModelFetchEnd = now() - t0;
                }
            }
            return response;
        };
    }

    // Hook EventSource so SSE message arrivals show up in the timeline.
    if (root.EventSource && !root.__eventSourceHooked) {
        root.__eventSourceHooked = true;
        const OrigEventSource = root.EventSource;
        const HookedEventSource = function (url, opts) {
            const es = new OrigEventSource(url, opts);
            log('sse.open', { url: String(url) });
            es.addEventListener('message', (e) => {
                let kind = '?';
                try {
                    const data = JSON.parse(e.data);
                    kind = data?.event || data?.type || data?.schema || '?';
                } catch (_error) { /* not JSON */ }
                log('sse.msg', { kind });
            });
            return es;
        };
        HookedEventSource.prototype = OrigEventSource.prototype;
        HookedEventSource.CONNECTING = OrigEventSource.CONNECTING;
        HookedEventSource.OPEN = OrigEventSource.OPEN;
        HookedEventSource.CLOSED = OrigEventSource.CLOSED;
        root.EventSource = HookedEventSource;
    }

    let summaryPrinted = false;
    const printSummary = () => {
        if (summaryPrinted) return;
        summaryPrinted = true;
        try {
            const c = root.console;
            if (!c) return;
            c.group('[flash-debug] boot summary');
            c.log('issue-card removals:', probe.issueCardsRemoved);
            c.log('column rebuilds:', probe.columnRebuilds);
            c.log('<html> attribute mutations:', probe.htmlAttrMutations.slice());
            c.log('<body> attribute mutations:', probe.bodyAttrMutations.slice());
            c.log('<body> direct child changes:', probe.bodyChildChanges);
            c.log('long animation frames (>50ms):', probe.longAnimationFrames.slice());
            c.log('stylesheet loads:', probe.stylesheetLoads.slice());
            c.log('first view-model fetch end (ms):', probe.firstViewModelFetchEnd);
            c.log('events:', probe.events.slice());
            const flashSignals = (
                probe.issueCardsRemoved
                + probe.columnRebuilds
                + probe.bodyChildChanges
                + probe.bodyAttrMutations.length
                + probe.longAnimationFrames.length
            );
            if (flashSignals > 0) {
                c.warn(
                    '[flash-debug] %d candidate flash signal(s) during boot. '
                    + 'Each long animation frame, body mutation, or card removal '
                    + 'above corresponds to something the user could see flash.',
                    flashSignals,
                );
            }
            c.groupEnd();
        } catch (_error) { /* console may be unavailable */ }
    };

    // Fallback: even if data-booting is never used (e.g. on the Control
    // Center, which doesn't include dashboard_boot.js), print a summary
    // after the page settles so the user always gets data.
    const FALLBACK_SUMMARY_MS = 3000;
    setTimeout(() => printSummary(), FALLBACK_SUMMARY_MS);
    if (root.addEventListener) {
        root.addEventListener('load', () => {
            // Wait one tick after load so any onload-triggered work lands.
            setTimeout(() => printSummary(), 250);
        }, { once: true });
    }

    if (typeof root.PerformanceObserver === 'function') {
        try {
            new root.PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    const t = Number((entry.startTime - t0).toFixed(1));
                    probe.longAnimationFrames.push({
                        t,
                        duration: Number(entry.duration.toFixed(1)),
                    });
                    log('frame', { duration: entry.duration });
                }
            }).observe({ type: 'long-animation-frame', buffered: true });
        } catch (_error) { /* LoAF not supported in this browser */ }
    }

    let documentElementObserved = false;
    let bodyObserved = false;
    let mainObserved = false;
    let stylesheetsObserved = false;

    const observeDocumentElement = () => {
        if (documentElementObserved) return;
        const documentElement = root.document?.documentElement;
        if (!documentElement) return;
        documentElementObserved = true;
        const initialBooting = documentElement.getAttribute('data-booting');
        if (initialBooting === 'true') {
            log('booting', { value: 'true', source: 'initial' });
        }
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                const attr = mutation.attributeName;
                if (!attr) continue;
                const newValue = documentElement.getAttribute(attr);
                const entry = {
                    t: Number((now() - t0).toFixed(1)),
                    attr,
                    oldValue: mutation.oldValue,
                    newValue,
                };
                probe.htmlAttrMutations.push(entry);
                log('html-attr', entry);
                // Emit a stable 'booting' event so the existing
                // booting-order assertion has a clean hook independent of
                // the broader html-attr stream.
                if (attr === 'data-booting') {
                    log('booting', { value: newValue });
                    if (newValue === null) {
                        probe.bootingCleared = true;
                        Promise.resolve().then(printSummary);
                    }
                }
            }
        }).observe(documentElement, { attributes: true, attributeOldValue: true });
    };

    // Body observer deliberately waits for DOMContentLoaded — before DCL,
    // the parser is streaming top-level children into <body>, and those
    // mutations are part of the initial paint, not a flash. After DCL,
    // any childList change to <body> is JS-driven and is a real flash
    // candidate.
    const observeBody = () => {
        if (bodyObserved) return false;
        const body = root.document?.body;
        if (!body) return false;
        if (root.document?.readyState === 'loading') return false;
        bodyObserved = true;
        const main = root.document?.querySelector('main#mainContent');
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                const target = mutation.target;
                // Skip mutations inside main — observeMain() already covers
                // those at higher fidelity and we don't want double-counts.
                if (main && main !== target && main.contains(target)) continue;
                const isDirectBody = target === body;
                if (mutation.type === 'attributes') {
                    const attr = mutation.attributeName;
                    const entry = {
                        t: Number((now() - t0).toFixed(1)),
                        target: isDirectBody ? 'body' : describeTarget(target),
                        attr,
                        oldValue: mutation.oldValue,
                        newValue: target.getAttribute?.(attr) ?? null,
                    };
                    if (isDirectBody) {
                        probe.bodyAttrMutations.push(entry);
                        log('body-attr', entry);
                    } else {
                        probe.bodyAttrSubtreeMutations.push(entry);
                        log('body-attr-subtree', entry);
                    }
                } else if (mutation.type === 'childList') {
                    const delta = mutation.addedNodes.length + mutation.removedNodes.length;
                    if (isDirectBody) {
                        probe.bodyChildChanges += delta;
                        log('body-child', {
                            target: describeTarget(target),
                            added: mutation.addedNodes.length,
                            removed: mutation.removedNodes.length,
                        });
                    } else {
                        probe.bodyChildSubtreeChanges += delta;
                        log('body-child-subtree', {
                            target: describeTarget(target),
                            added: mutation.addedNodes.length,
                            removed: mutation.removedNodes.length,
                        });
                    }
                }
            }
        }).observe(body, {
            attributes: true,
            attributeOldValue: true,
            childList: true,
            subtree: true,
        });
        return true;
    };

    const observeStylesheets = () => {
        if (stylesheetsObserved) return;
        try {
            const links = root.document?.querySelectorAll('link[rel="stylesheet"]');
            if (!links) return;
            stylesheetsObserved = true;
            for (const link of links) {
                const stamp = (eventKind) => {
                    probe.stylesheetLoads.push({
                        t: Number((now() - t0).toFixed(1)),
                        href: link.getAttribute('href'),
                        kind: eventKind,
                    });
                };
                if (link.sheet) {
                    stamp('already-loaded');
                } else {
                    link.addEventListener('load', () => stamp('load'), { once: true });
                    link.addEventListener('error', () => stamp('error'), { once: true });
                }
            }
        } catch (_error) { /* ignore */ }
    };

    const observeMain = () => {
        if (mainObserved) return false;
        const main = root.document?.querySelector('main#mainContent');
        if (!main) return false;
        mainObserved = true;
        const isCard = (el) => el?.nodeType === 1 && el.classList?.contains('issue-card');
        const isColumn = (el) => el?.nodeType === 1 && el.classList?.contains('column-cards');
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                if (mutation.type === 'attributes') {
                    const target = mutation.target;
                    const attr = mutation.attributeName;
                    if (isCard(target)) {
                        probe.cardAttrMutations += 1;
                        log('mutation', { kind: 'issue-card.attr', attr });
                    } else {
                        const entry = {
                            t: Number((now() - t0).toFixed(1)),
                            target: describeTarget(target),
                            attr,
                            oldValue: mutation.oldValue,
                            newValue: target.getAttribute?.(attr) ?? null,
                        };
                        probe.mainAttrMutations.push(entry);
                        log('main-attr', entry);
                    }
                    continue;
                }
                for (const added of mutation.addedNodes) {
                    if (isCard(added)) {
                        probe.issueCardsAdded += 1;
                        log('mutation', { kind: 'issue-card.added' });
                    } else if (isColumn(added)) {
                        probe.columnInserts += 1;
                        log('mutation', { kind: 'column-cards.added' });
                    }
                }
                for (const removed of mutation.removedNodes) {
                    if (isCard(removed)) {
                        probe.issueCardsRemoved += 1;
                        log('mutation', { kind: 'issue-card.removed' });
                    } else if (isColumn(removed)) {
                        probe.columnRebuilds += 1;
                        log('mutation', { kind: 'column-cards.removed' });
                    }
                }
            }
        }).observe(main, {
            childList: true,
            subtree: true,
            attributes: true,
        });
        return true;
    };

    const installObservers = () => {
        observeDocumentElement();
        observeStylesheets();
        if (observeMain()) {
            probe.ready = true;
            return true;
        }
        return false;
    };

    if (!installObservers()) {
        const interval = setInterval(() => {
            if (installObservers()) clearInterval(interval);
        }, 5);
        setTimeout(() => clearInterval(interval), 4000);
    }

    // Body observer is gated on DOMContentLoaded so parser-driven inserts
    // (header, container, main, etc.) don't count as flashes.
    if (root.document?.readyState !== 'loading') {
        observeBody();
    } else {
        root.document.addEventListener('DOMContentLoaded', () => observeBody(), { once: true });
    }
}());
