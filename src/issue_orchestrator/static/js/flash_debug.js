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
        issueCardsRemoved: 0,
        columnRebuilds: 0,
        htmlAttrMutations: [],
        bodyAttrMutations: [],
        bodyChildChanges: 0,
        longAnimationFrames: [],
        stylesheetLoads: [],
        ready: false,
        events: [],
        bootingCleared: false,
        firstViewModelFetchEnd: null,
    };
    root.__cardFlash = probe;

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
            const isVm = url.includes('/api/view-model');
            if (isVm) log('fetch.start', { url });
            const response = await origFetch(...args);
            if (isVm) {
                log('fetch.end', { url, status: response.status });
                if (probe.firstViewModelFetchEnd === null) {
                    probe.firstViewModelFetchEnd = now() - t0;
                }
            }
            return response;
        };
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
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                if (mutation.type === 'attributes') {
                    const attr = mutation.attributeName;
                    const newValue = attr ? body.getAttribute(attr) : null;
                    const entry = {
                        t: Number((now() - t0).toFixed(1)),
                        attr,
                        oldValue: mutation.oldValue,
                        newValue,
                    };
                    probe.bodyAttrMutations.push(entry);
                    log('body-attr', entry);
                } else if (mutation.type === 'childList') {
                    probe.bodyChildChanges
                        += mutation.addedNodes.length + mutation.removedNodes.length;
                    log('body-child', {
                        added: mutation.addedNodes.length,
                        removed: mutation.removedNodes.length,
                    });
                }
            }
        }).observe(body, {
            attributes: true,
            attributeOldValue: true,
            childList: true,
        });
        return true;
    };

    const observeStylesheets = () => {
        try {
            const links = root.document?.querySelectorAll('link[rel="stylesheet"]');
            if (!links) return;
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
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                for (const removed of mutation.removedNodes) {
                    if (removed?.nodeType !== 1) continue;
                    if (removed.classList?.contains('issue-card')) {
                        probe.issueCardsRemoved += 1;
                        log('mutation', { kind: 'issue-card.removed' });
                    }
                    if (removed.classList?.contains('column-cards')) {
                        probe.columnRebuilds += 1;
                        log('mutation', { kind: 'column-cards.removed' });
                    }
                }
            }
        }).observe(main, { childList: true, subtree: true });
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
