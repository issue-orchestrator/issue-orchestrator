// Canonical JUnit / validation viewer + Phase-0 plugin registry.
//
// This module renders the rich validation-results body that the
// validation dialog (and, in Phase B, the per-issue drawer's
// "Validation" cycle event; and Phase C, the E2E run view) shares.
//
// Inputs: a ``ValidationFailureDialogPayload`` (despite the historical
// name, the payload covers both passed and failed runs).  The key
// per-test data is ``data.junit_cases`` — a list of
// ``JUnitCasePayload`` objects.
//
// Each junit case may carry ``case.extras: [{namespace, payload}, ...]``
// (Phase-0 plugin slot).  For each extra, we look up a renderer
// registered for the namespace and embed the result below the case's
// per-test detail.  Unknown namespaces silently skip — the design
// boundary is "the viewer is generic; orchestrator-specific concepts
// live in plugin modules registered by issue-orchestrator's dashboard".
// See ``docs/journeys/validation-viewer-redesign.md`` for the why.
//
// Phase-0 scope (this commit) — what's here:
//   * registry: ``registerValidationPlugin(namespace, renderer)``,
//     ``renderPluginExtras(case)``.
//   * canonical viewer: ``renderCanonicalValidationViewer(data, options)``.
//     ``options.renderActionSections`` is an explicit dependency — when
//     ``data.action_sections`` is non-empty and the caller wants a
//     "Validation artifacts" footer, it passes a renderer.  The viewer
//     never reaches into other dashboard modules' globals.
// What's NOT here (deliberate Phase-0 limits — see redesign doc):
//   * stdout marker protocol (we own the parser; case.extras is fine)
//   * plugin manifest / dynamic loading
//   * version negotiation in namespaces
//   * fallback "unknown plugin" UI

// ── Plugin registry ─────────────────────────────────────────────────────────

const _validationPluginRegistry = Object.create(null);

function registerValidationPlugin(namespace, renderer) {
    if (typeof namespace !== 'string' || !namespace) {
        throw new Error('registerValidationPlugin: namespace must be a non-empty string');
    }
    if (typeof renderer !== 'function') {
        throw new Error(`registerValidationPlugin: renderer for ${namespace} must be a function`);
    }
    _validationPluginRegistry[namespace] = renderer;
}

function getValidationPlugin(namespace) {
    return _validationPluginRegistry[namespace] || null;
}

function renderPluginExtras(testCase) {
    const extras = Array.isArray(testCase && testCase.extras) ? testCase.extras : [];
    if (extras.length === 0) return '';
    const parts = [];
    for (const extra of extras) {
        if (!extra || typeof extra !== 'object') continue;
        const renderer = _validationPluginRegistry[extra.namespace];
        if (!renderer) continue;  // unknown namespace: silently skip
        try {
            const html = renderer(extra.payload, testCase);
            if (typeof html === 'string' && html) parts.push(html);
        } catch (err) {
            // A misbehaving plugin must not crash the whole viewer.
            // Show a single-line error inline so the bug is visible to
            // the user instead of vanishing.
            const msg = err && err.message ? err.message : String(err);
            parts.push(`<div class="diag-validation-plugin-error" data-namespace="${escapeAttr(extra.namespace)}">Plugin <code>${escapeHtml(extra.namespace)}</code> failed to render: ${escapeHtml(msg)}</div>`);
        }
    }
    return parts.join('');
}

// Test-only hook so JS-vm tests can reset the registry between cases
// without depending on module reload semantics.
function _resetValidationPluginRegistryForTests() {
    for (const k of Object.keys(_validationPluginRegistry)) {
        delete _validationPluginRegistry[k];
    }
}

// ── Canonical viewer ────────────────────────────────────────────────────────

// ``options`` is the viewer's explicit dependency boundary (issue #6310
// follow-up reviewer Blocker 2).  Callers that want artifact actions
// rendered MUST pass ``options.renderActionSections`` — the viewer no
// longer reaches into session_dialogs.js globals.  Tests that need
// action-section coverage pass a stub; tests that don't can leave it
// unset and the artifacts section is simply omitted.
function renderCanonicalValidationViewer(data, options = {}) {
    // Tolerate partial payloads: production always sends the full shape
    // (the route validates against ``ValidationFailureDialogPayload``),
    // but JS-vm tests + the per-event embed in Phase B may pass slimmer
    // objects.  Default arrays to empty.
    const cases = Array.isArray(data && data.junit_cases) ? data.junit_cases : [];
    const failedTests = Array.isArray(data && data.failed_tests) ? data.failed_tests : [];
    const stdoutExcerpt = Array.isArray(data && data.stdout_excerpt) ? data.stdout_excerpt : [];
    const stderrExcerpt = Array.isArray(data && data.stderr_excerpt) ? data.stderr_excerpt : [];
    const actionSections = Array.isArray(data && data.action_sections) ? data.action_sections : [];
    const renderActionSections = (options && typeof options.renderActionSections === 'function')
        ? options.renderActionSections
        : null;
    const status = (data && data.status === 'passed') ? 'passed' : 'failed';

    const failureCases = cases.filter((c) => c && (c.outcome === 'failed' || c.outcome === 'error'));
    const otherCases = cases.filter((c) => c && c.outcome !== 'failed' && c.outcome !== 'error');

    // failed_tests fallback (reviewer Blocker 1): when JUnit XML wasn't
    // available — e.g. the runner died before writing it, or the suite
    // isn't JUnit-configured — the endpoint still reports failures via
    // the legacy ``failed_tests`` string list (node IDs).  Without this
    // fallback those IDs disappear from the rendered dialog (only the
    // chip-row count survives).  Synthesize minimal failed cases for any
    // node ID not already represented in ``cases``, so each failing test
    // gets a triage card and the operator can see *what* failed.
    const representedNodeIds = new Set();
    for (const c of cases) {
        if (c && typeof c.case_id === 'string') representedNodeIds.add(c.case_id);
        if (c && typeof c.display_name === 'string') representedNodeIds.add(c.display_name);
    }
    for (const nodeId of failedTests) {
        const id = String(nodeId || '').trim();
        if (!id || representedNodeIds.has(id)) continue;
        representedNodeIds.add(id);
        failureCases.push(_synthesizeFailedCaseFromNodeId(id));
    }

    // ``role="tree"`` + the per-row ``role="treeitem"`` + per-body
    // ``role="group"`` give the viewer a textbook ARIA tree shape
    // (issue #6310 follow-up Phase D).  ``aria-orientation="vertical"``
    // pins keyboard nav semantics; ``aria-label`` gives the tree a name
    // for screen readers.  Initial ``aria-level``/``aria-setsize``/
    // ``aria-posinset`` values are filled in by
    // ``enhanceCanonicalValidationViewerAccessibility`` after mount
    // because they require live DOM context.
    let html = '<div class="cvv-root" data-cvv-status="' + escapeAttr(status) + '"'
        + ' role="tree" aria-orientation="vertical"'
        + ' aria-label="Validation results">';

    // Triage: failed/errored tests as cards at the top, auto-expanded.
    if (failureCases.length > 0) {
        html += '<section class="cvv-triage">';
        for (let i = 0; i < failureCases.length; i++) {
            html += _renderTriageCard(failureCases[i], `cvv-fail-${i}`);
        }
        html += '</section>';
    }

    // Browse-by-file for non-failed cases (passed/skipped).  Single
    // top-level expander; clicking opens the file list.  Each file
    // expands to test rows; each test expands to its stdout / duration /
    // sparkline.
    if (otherCases.length > 0) {
        const passedCount = otherCases.filter((c) => c.outcome === 'passed').length;
        const skippedCount = otherCases.filter((c) => c.outcome === 'skipped').length;
        const summaryParts = [];
        if (passedCount > 0) summaryParts.push(`${passedCount} passed`);
        if (skippedCount > 0) summaryParts.push(`${skippedCount} skipped`);
        const summary = (failureCases.length > 0 ? '+ ' : '') + summaryParts.join(', ');
        html += '<section class="cvv-browse">';
        const browseOpen = failureCases.length === 0;
        html += `<details class="cvv-row cvv-row-browse" role="treeitem" aria-expanded="${browseOpen ? 'true' : 'false'}" ${browseOpen ? 'open' : ''}>`;
        html += `<summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-passed">✓</span><span class="cvv-title">${escapeHtml(summary)}</span><span class="cvv-summary">browse by file</span></summary>`;
        html += '<div class="cvv-row-body" role="group">';
        html += _renderBrowseByFile(otherCases, 'cvv-browse');
        html += '</div></details>';
        html += '</section>';
    }

    // stdout/stderr excerpts — preserved from the historical dialog
    // shape for callers that still want them.  Per-test cases already
    // have their own stdout/stderr in the triage cards / per-test
    // expansion above, but the *run-level* excerpts are different (they
    // capture orchestrator-side stdout, not per-test).  Render them in
    // a collapsed footer so they don't compete with the test detail.
    if (stdoutExcerpt.length > 0 || stderrExcerpt.length > 0) {
        html += '<section class="cvv-run-output">';
        if (stdoutExcerpt.length > 0) {
            html += `<details class="cvv-row" role="treeitem" aria-expanded="false"><summary><span class="cvv-caret">▸</span><span class="cvv-title">Run stdout</span><span class="cvv-summary">${stdoutExcerpt.length} line${stdoutExcerpt.length === 1 ? '' : 's'}</span></summary>`;
            html += `<pre class="cvv-pre">${escapeHtml(stdoutExcerpt.join('\n'))}</pre>`;
            html += '</details>';
        }
        if (stderrExcerpt.length > 0) {
            html += `<details class="cvv-row" role="treeitem" aria-expanded="false" ${status === 'failed' ? '' : ''}><summary><span class="cvv-caret">▸</span><span class="cvv-title">Run stderr</span><span class="cvv-summary">${stderrExcerpt.length} line${stderrExcerpt.length === 1 ? '' : 's'}</span></summary>`;
            html += `<pre class="cvv-pre">${escapeHtml(stderrExcerpt.join('\n'))}</pre>`;
            html += '</details>';
        }
        html += '</section>';
    }

    // Validation artifacts (record / output / stderr / session evidence
    // / diagnostics) — historical action_sections, rendered as a
    // collapsed footer so the user's eye lands on tests first.  Action
    // *button* rendering belongs to dashboard-wide code (each button
    // type has its own onclick handler), so the viewer takes a renderer
    // via ``options.renderActionSections`` instead of reaching out to a
    // global.  If no renderer was passed, the section is omitted — the
    // payload data is still present for callers that want to render
    // their own footer.
    if (actionSections.length > 0 && renderActionSections) {
        html += '<section class="cvv-artifacts">';
        html += '<details class="cvv-row" role="treeitem" aria-expanded="false"><summary><span class="cvv-caret">▸</span><span class="cvv-title">Validation artifacts</span><span class="cvv-summary">record · output · evidence</span></summary>';
        html += '<div class="cvv-row-body" role="group">';
        html += renderActionSections(actionSections);
        html += '</div></details>';
        html += '</section>';
    }

    html += '</div>';  // cvv-root
    return html;
}

// ── Accessibility enhancer (issue #6310 follow-up Phase D) ──────────────────
//
// Renders the canonical viewer with the right ARIA roles baked into the
// HTML (``role="tree"`` on the root, ``role="treeitem"`` on each row,
// ``role="group"`` on each row's body).  The render-time pass can't
// compute ``aria-level``/``aria-setsize``/``aria-posinset`` because they
// depend on the live DOM (nesting depth + sibling counts).  Callers
// (modal / drawer / E2E view) invoke this after mounting the HTML.
//
// The enhancer is idempotent: re-running it on the same DOM is safe.
//
// Keyboard nav contract (matches WAI-ARIA Authoring Practices for tree):
//   * ArrowDown   → focus next visible treeitem.
//   * ArrowUp     → focus previous visible treeitem.
//   * ArrowRight  → if collapsed, expand; else focus first child.
//   * ArrowLeft   → if expanded, collapse; else focus parent treeitem.
//   * Home / End  → focus first / last visible treeitem.
//   * Enter / Space → toggle expansion.
//
// The keyboard pipeline is split into three pieces so the semantics
// are testable without a real browser (reviewer Blocker 2 on PR #6316):
//
//   1. ``_treeCommandForKey(key, viewState) → command | null``
//      A pure function that translates a key + a tiny ``viewState``
//      ({isDetails, isOpen}) into a tree command — one of
//      ``next`` / ``prev`` / ``expand`` / ``collapse`` /
//      ``focus-first-child`` / ``focus-parent`` / ``first`` /
//      ``last`` / ``toggle``.  JS-vm tests cover the matrix.
//
//   2. ``_executeTreeCommand(command, item, root, ops) → boolean``
//      Applies the command via a small ``ops`` adapter that
//      abstracts the DOM operations the command needs.  Production
//      uses ``_DOM_TREE_OPS`` (real-DOM adapter); JS-vm tests pass a
//      fake adapter backed by a plain-JS tree fixture so traversal,
//      expand/collapse, parent/child focus, and roving-tabindex
//      results are verified without a browser.
//
//   3. ``_onTreeKeydown(event)`` — the bound listener.  Thin wrapper
//      that translates the key, executes the command, and prevents
//      default on a successful command.

function enhanceCanonicalValidationViewerAccessibility(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    // Walk the tree: for each treeitem, compute aria-level (nesting),
    // aria-setsize (# of treeitem siblings under its parent group), and
    // aria-posinset (1-indexed position).
    const treeitems = root.querySelectorAll('[role="treeitem"]');
    for (const item of treeitems) {
        const level = _computeTreeitemLevel(item, root);
        item.setAttribute('aria-level', String(level));
        const siblings = _treeitemSiblings(item);
        item.setAttribute('aria-setsize', String(siblings.length));
        const pos = siblings.indexOf(item) + 1;
        if (pos > 0) item.setAttribute('aria-posinset', String(pos));
        // Tab-stop discipline: only one treeitem is in the tab order at
        // a time (the focused one).  Pre-mount we put the first
        // treeitem in tab order and the rest at -1 — arrow keys move
        // focus from there.
        if (!item.hasAttribute('tabindex')) {
            item.tabIndex = (item === treeitems[0]) ? 0 : -1;
        }
        // Sync aria-expanded with the underlying <details>.open state
        // (the open attribute may have changed between render and
        // enhance, e.g. browsers restoring scroll position).
        if (item.tagName === 'DETAILS') {
            item.setAttribute('aria-expanded', item.open ? 'true' : 'false');
        }
    }

    // One delegated keydown listener covers the whole tree.  Mark the
    // root so re-enhancing doesn't stack listeners.
    if (root.dataset.cvvA11yBound !== '1') {
        root.addEventListener('keydown', _onTreeKeydown);
        root.addEventListener('toggle', _onTreeToggle, true);
        root.dataset.cvvA11yBound = '1';
    }
}

function _computeTreeitemLevel(item, root) {
    let level = 1;
    let parent = item.parentElement;
    while (parent && parent !== root) {
        if (parent.getAttribute && parent.getAttribute('role') === 'treeitem') level++;
        parent = parent.parentElement;
    }
    return level;
}

function _treeitemSiblings(item) {
    // Siblings are the treeitems sharing the same parent treeitem (or
    // sharing the tree root if this is a top-level item).  ``cvv-row +
    // cvv-row`` is the visual sibling relationship; we walk up to the
    // closest ``role="group"`` (or ``role="tree"``) and gather its
    // direct ``role="treeitem"`` descendants.
    const parentGroup = item.parentElement && item.parentElement.closest('[role="group"], [role="tree"]');
    if (!parentGroup) return [item];
    const all = parentGroup.querySelectorAll(':scope > [role="treeitem"], :scope > section > [role="treeitem"], :scope > section > details[role="treeitem"]');
    return Array.from(all);
}

function _onTreeToggle(event) {
    // <details> fires a ``toggle`` event when open flips.  Keep the
    // ARIA state in sync so screen readers report it correctly.
    const target = event.target;
    if (target && target.tagName === 'DETAILS' && target.getAttribute('role') === 'treeitem') {
        target.setAttribute('aria-expanded', target.open ? 'true' : 'false');
    }
}

// ── Layer 1: pure key → command translation (testable in JS-vm) ─────

function _treeCommandForKey(key, viewState) {
    // ``viewState`` is the minimum a translation needs to know:
    //   * ``isDetails`` — whether the focused treeitem is a <details>
    //     element (and therefore can expand/collapse/toggle).
    //   * ``isOpen``    — whether that <details> is currently open.
    // Returning ``null`` means the key is not a tree-nav binding and
    // the event handler should not preventDefault.
    const state = viewState || {};
    switch (key) {
        case 'ArrowDown': return 'next';
        case 'ArrowUp': return 'prev';
        case 'ArrowRight':
            if (state.isDetails && !state.isOpen) return 'expand';
            return 'focus-first-child';
        case 'ArrowLeft':
            if (state.isDetails && state.isOpen) return 'collapse';
            return 'focus-parent';
        case 'Home': return 'first';
        case 'End': return 'last';
        case 'Enter':
        case ' ':
            if (state.isDetails) return 'toggle';
            return null;
        default:
            return null;
    }
}

// ── Layer 2: command executor with dependency-injected DOM ops ──────

function _executeTreeCommand(command, item, root, ops) {
    switch (command) {
        case 'next': {
            const next = ops.nextVisible(item, root);
            if (!next) return false;
            ops.focusItem(next, root);
            return true;
        }
        case 'prev': {
            const prev = ops.prevVisible(item, root);
            if (!prev) return false;
            ops.focusItem(prev, root);
            return true;
        }
        case 'expand': {
            ops.setOpen(item, true);
            return true;
        }
        case 'collapse': {
            ops.setOpen(item, false);
            return true;
        }
        case 'focus-first-child': {
            const child = ops.firstChild(item);
            if (!child) return false;
            ops.focusItem(child, root);
            return true;
        }
        case 'focus-parent': {
            const parent = ops.parent(item, root);
            if (!parent) return false;
            ops.focusItem(parent, root);
            return true;
        }
        case 'first': {
            const first = ops.firstVisible(root);
            if (!first) return false;
            ops.focusItem(first, root);
            return true;
        }
        case 'last': {
            const last = ops.lastVisible(root);
            if (!last) return false;
            ops.focusItem(last, root);
            return true;
        }
        case 'toggle': {
            ops.setOpen(item, !ops.getOpen(item));
            return true;
        }
        default:
            return false;
    }
}

// Production adapter — the real-DOM implementation of ``ops``.  Each
// method is a thin wrapper around the small set of DOM operations the
// commands need.  JS-vm tests pass an alternative adapter; the
// production listener uses this one.
const _DOM_TREE_OPS = {
    nextVisible: (item, root) => _nextVisibleTreeitem(item, root),
    prevVisible: (item, root) => _prevVisibleTreeitem(item, root),
    firstVisible: (root) => {
        const all = _visibleTreeitems(root);
        return all.length > 0 ? all[0] : null;
    },
    lastVisible: (root) => {
        const all = _visibleTreeitems(root);
        return all.length > 0 ? all[all.length - 1] : null;
    },
    firstChild: (item) =>
        item.querySelector(':scope > [role="group"] [role="treeitem"], :scope [role="group"] > [role="treeitem"]'),
    parent: (item) =>
        item.parentElement ? item.parentElement.closest('[role="treeitem"]') : null,
    setOpen: (item, value) => {
        if (item.tagName === 'DETAILS') item.open = !!value;
    },
    getOpen: (item) => !!(item.tagName === 'DETAILS' && item.open),
    focusItem: (item, root) => _focusTreeitem(item, root),
};

// ── Layer 3: thin event listener ────────────────────────────────────

function _onTreeKeydown(event) {
    const item = event.target && event.target.closest && event.target.closest('[role="treeitem"]');
    if (!item) return;
    const viewState = {
        isDetails: item.tagName === 'DETAILS',
        isOpen: !!(item.tagName === 'DETAILS' && item.open),
    };
    const command = _treeCommandForKey(event.key, viewState);
    if (!command) return;
    const handled = _executeTreeCommand(command, item, event.currentTarget, _DOM_TREE_OPS);
    if (handled) {
        event.preventDefault();
        event.stopPropagation();
    }
}

// ── DOM-backed helpers (used by the production ops adapter) ─────────

function _visibleTreeitems(root) {
    // A treeitem is "visible" if every ancestor treeitem (within the
    // tree) is open.  Walk every treeitem and filter.
    const all = Array.from(root.querySelectorAll('[role="treeitem"]'));
    return all.filter((it) => _isTreeitemVisible(it, root));
}

function _isTreeitemVisible(item, root) {
    let parent = item.parentElement;
    while (parent && parent !== root) {
        if (parent.getAttribute && parent.getAttribute('role') === 'treeitem') {
            if (parent.tagName === 'DETAILS' && !parent.open) return false;
        }
        parent = parent.parentElement;
    }
    return true;
}

function _nextVisibleTreeitem(current, root) {
    const all = _visibleTreeitems(root);
    const idx = all.indexOf(current);
    return idx >= 0 && idx < all.length - 1 ? all[idx + 1] : null;
}

function _prevVisibleTreeitem(current, root) {
    const all = _visibleTreeitems(root);
    const idx = all.indexOf(current);
    return idx > 0 ? all[idx - 1] : null;
}

function _focusTreeitem(item, root) {
    // Roving tabindex: only the focused item is in the tab order.
    const all = root.querySelectorAll('[role="treeitem"]');
    for (const it of all) it.tabIndex = -1;
    item.tabIndex = 0;
    if (typeof item.focus === 'function') item.focus();
}

// ── Triage card (one failed/errored test, auto-expanded) ────────────────────

function _renderTriageCard(testCase, idPrefix) {
    const outcome = testCase.outcome === 'error' ? 'error' : 'failed';
    const headlineKind = outcome === 'error' ? 'is-error' : 'is-failed';
    const displayName = String(testCase.display_name || testCase.case_id || '(unnamed test)');
    const suiteName = testCase.suite_name ? String(testCase.suite_name) : '';
    const duration = _formatDuration(testCase.duration_seconds);

    // Layout selector (Phase C / 3a): a 1-line failure renders the
    // headline message inline next to the test name, saving vertical
    // space on cards that have no traceback to drill into.  A
    // multi-line failure keeps the red headline box + traceback row.
    const layout = _failureCardLayoutForCase(testCase);

    // The triage card is a ``role="group"`` so its child treeitems
    // (traceback / stdout / stderr) share the same tree/group ownership
    // model as the browse rows (reviewer Blocker 1 on PR #6316).
    // Without this role the children's parent group resolves all the
    // way up to ``.cvv-root[role=tree]``, which leaks both
    // ``aria-setsize`` (counted against unrelated top-level rows) and
    // ``aria-posinset`` (the children aren't members of that distant
    // sibling set, so the enhancer can't assign a position).
    let html = `<div class="cvv-triage-card cvv-${outcome} cvv-layout-${layout.variant}" role="group">`;

    html += '<div class="cvv-triage-head">';
    html += `<span class="cvv-ico cvv-ico-${outcome}">${outcome === 'error' ? '⚠' : '✕'}</span>`;
    html += `<span class="cvv-triage-title">${escapeHtml(displayName)}</span>`;
    if (suiteName) html += `<span class="cvv-summary">${escapeHtml(suiteName)}</span>`;
    // 3a: inline-variant headline lives in the head row so it scans on
    // one line.  Truncates via CSS if it overflows.
    if (layout.variant === 'inline' && layout.headlineMessage) {
        html += `<span class="cvv-inline-headline ${headlineKind}" title="${escapeAttr(layout.headlineMessage)}">${escapeHtml(layout.headlineMessage)}</span>`;
    }
    // Copy-error built-in icon: generic to every failed card,
    // independent of any plugin.  Lives next to the test name so it's
    // scan-accessible whether the card is opened or not.
    if (testCase.failure_details) {
        const copyPayload = escapeAttr(String(testCase.failure_details));
        html += `<button class="cvv-copy-icon" type="button" title="Copy error" aria-label="Copy error text to clipboard" data-cvv-copy-text="${copyPayload}" onclick="event.stopPropagation(); _cvvCopyErrorFromButton(this);">⎘</button>`;
    }
    html += '</div>';

    // two-row variant: keep the existing red-headline box.
    if (layout.variant === 'two-row' && layout.headlineMessage) {
        html += `<div class="cvv-headline ${headlineKind}"><span class="cvv-headline-text">${escapeHtml(layout.headlineMessage)}</span></div>`;
    }

    html += '<div class="cvv-badges">';
    html += `<span class="cvv-chip cvv-chip-${outcome}">${outcome === 'error' ? '⚠ Errored' : '✕ Failed'}</span>`;
    if (duration) html += `<span class="cvv-chip">${escapeHtml(duration)}</span>`;
    html += '</div>';

    // traceback row only when there's a body to show (two-row variant).
    if (layout.variant === 'two-row' && layout.tracebackBody) {
        html += `<details class="cvv-row" role="treeitem" aria-expanded="true" open><summary><span class="cvv-caret">▸</span><span class="cvv-title">traceback</span></summary>`;
        html += `<pre class="cvv-pre cvv-pre-fail">${escapeHtml(layout.tracebackBody)}</pre>`;
        html += '</details>';
    }

    html += _renderTestSystemOutErr(testCase, idPrefix, outcome === 'error');

    // Plugin extras: render below the test detail, before any closing
    // actions row.  The io.agent-context plugin renders here for E2E
    // tests carrying linked-issue data in ``extras``.
    html += renderPluginExtras(testCase);

    html += '</div>';  // cvv-triage-card
    return html;
}

// Built-in Copy-error click handler.  Reads the failure text out of the
// ``data-cvv-copy-text`` attribute and writes it to the clipboard.  No
// plugin dependency — generic.  Falls back to a tiny inline message if
// the Clipboard API isn't available.
function _cvvCopyErrorFromButton(button) {
    if (!button) return;
    const text = button.getAttribute('data-cvv-copy-text') || '';
    if (!text) return;
    const navClip = (typeof navigator !== 'undefined') ? navigator.clipboard : null;
    if (navClip && typeof navClip.writeText === 'function') {
        navClip.writeText(text).catch(() => { /* swallow */ });
    }
    // Brief visual ack on the button.
    const original = button.textContent;
    button.textContent = '✓';
    setTimeout(() => { button.textContent = original; }, 900);
}

// ── Browse-by-file (passed + skipped) ───────────────────────────────────────

function _renderBrowseByFile(cases, idPrefix) {
    const byFile = new Map();
    for (const c of cases) {
        const key = String(c.suite_name || '(unknown file)');
        if (!byFile.has(key)) byFile.set(key, []);
        byFile.get(key).push(c);
    }
    const files = Array.from(byFile.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    if (files.length === 0) return '';
    let html = '';
    for (let i = 0; i < files.length; i++) {
        const [fileName, items] = files[i];
        const passCount = items.filter((c) => c.outcome === 'passed').length;
        const skipCount = items.filter((c) => c.outcome === 'skipped').length;
        const totalMs = items.reduce((s, c) => s + ((typeof c.duration_seconds === 'number') ? c.duration_seconds * 1000 : 0), 0);
        const statsParts = [];
        if (passCount > 0) statsParts.push(`${passCount} passed`);
        if (skipCount > 0) statsParts.push(`${skipCount} skipped`);
        if (totalMs > 0) statsParts.push(_formatMs(totalMs));
        const stats = statsParts.join(' · ');
        const base = fileName.split('/').pop();
        const dirPart = fileName.length > base.length ? fileName.slice(0, fileName.length - base.length - 1) : '';
        html += `<details class="cvv-row cvv-file" role="treeitem" aria-expanded="false"><summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-passed">✓</span><span class="cvv-title">${escapeHtml(base)}</span>${dirPart ? `<span class="cvv-summary">${escapeHtml(dirPart)}</span>` : ''}<span class="cvv-meta">${escapeHtml(stats)}</span></summary>`;
        html += '<div class="cvv-row-body" role="group">';
        for (let j = 0; j < items.length; j++) {
            html += _renderPassedTestRow(items[j], `${idPrefix}-f${i}-t${j}`);
        }
        html += '</div></details>';
    }
    return html;
}

function _renderPassedTestRow(testCase, idPrefix) {
    const displayName = String(testCase.display_name || testCase.case_id || '(unnamed test)');
    const duration = _formatDuration(testCase.duration_seconds);
    const outcome = testCase.outcome === 'skipped' ? 'skipped' : 'passed';
    const outcomeIcon = outcome === 'skipped' ? '–' : '✓';

    let html = `<details class="cvv-row cvv-test" role="treeitem" aria-expanded="false">`;
    html += `<summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-${outcome}">${outcomeIcon}</span><span class="cvv-title">${escapeHtml(displayName)}</span>${duration ? `<span class="cvv-meta">${escapeHtml(duration)}</span>` : ''}</summary>`;
    html += '<div class="cvv-row-body" role="group">';
    html += '<div class="cvv-badges">';
    html += `<span class="cvv-chip cvv-chip-${outcome}">${outcome === 'skipped' ? '– Skipped' : '✓ Passed'}</span>`;
    if (duration) html += `<span class="cvv-chip">${escapeHtml(duration)}</span>`;
    html += '</div>';
    // Skipped tests carry their skip reason in ``failure_details``
    // (the JUnit parser stores ``<skipped message="..."/>`` and any
    // inline body text there).  Surface it inline so a user
    // expanding a skipped row can see *why* without leaving the
    // dashboard.  Passed tests have no equivalent — they're just
    // green.
    if (outcome === 'skipped') {
        const skipReason = String(testCase.failure_details || '').trim();
        if (skipReason) {
            html += `<div class="cvv-skip-reason">${escapeHtml(skipReason)}</div>`;
        } else {
            html += '<div class="cvv-empty">No skip reason was recorded for this test.</div>';
        }
    }
    html += _renderTestSystemOutErr(testCase, idPrefix, false);
    html += renderPluginExtras(testCase);
    html += '</div></details>';
    return html;
}

function _renderTestSystemOutErr(testCase, idPrefix, errorOpenStderr) {
    let html = '';
    const stdout = testCase.system_out || '';
    const stderr = testCase.system_err || '';
    const stdoutLines = stdout ? stdout.split('\n').filter((l) => l.length > 0).length : 0;
    const stderrLines = stderr ? stderr.split('\n').filter((l) => l.length > 0).length : 0;

    html += `<details class="cvv-row" role="treeitem" aria-expanded="false"><summary><span class="cvv-caret">▸</span><span class="cvv-title">stdout</span><span class="cvv-summary">${stdoutLines === 0 ? 'empty' : `${stdoutLines} line${stdoutLines === 1 ? '' : 's'}`}</span></summary>`;
    html += stdout ? `<pre class="cvv-pre">${escapeHtml(stdout)}</pre>` : '<div class="cvv-empty">No stdout captured.</div>';
    html += '</details>';

    const stderrOpen = !!(errorOpenStderr && stderr);
    html += `<details class="cvv-row" role="treeitem" aria-expanded="${stderrOpen ? 'true' : 'false'}"${stderrOpen ? ' open' : ''}><summary><span class="cvv-caret">▸</span><span class="cvv-title">stderr</span><span class="cvv-summary">${stderrLines === 0 ? 'empty' : `${stderrLines} line${stderrLines === 1 ? '' : 's'}`}</span></summary>`;
    html += stderr ? `<pre class="cvv-pre">${escapeHtml(stderr)}</pre>` : '<div class="cvv-empty">No stderr captured.</div>';
    html += '</details>';

    return html;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

// Build a minimal failed JUnitCasePayload-shaped object from a raw
// node-ID string.  Used when the endpoint reports failures via the
// legacy ``failed_tests`` list but ``junit_cases`` is empty or doesn't
// cover the ID (reviewer Blocker 1 fallback).  The triage card
// downgrades gracefully: no traceback, no system_out/err — just the
// node ID + a "from validation log" headline so the operator knows
// where the data came from.
function _synthesizeFailedCaseFromNodeId(nodeId) {
    return {
        case_id: nodeId,
        display_name: nodeId,
        outcome: 'failed',
        duration_seconds: null,
        suite_name: '',
        failure_details: 'Reported as failed by the validation runner. No JUnit XML detail was available — see the run stdout/stderr expanders below or open the validation record for full output.',
        system_out: '',
        system_err: '',
        extras: [],
        _synthesized_from_failed_tests: true,
    };
}

// Pure layout selector for failure cards (Phase C / 3a).  Returns one of
// two variants:
//   * ``inline``   — the failure has no traceback body; render the
//                    headline message next to the test name in a tight
//                    single-row card.  Saves vertical space; preserves
//                    diagnostic info at scan time.
//   * ``two-row``  — the failure has multi-line content; render the
//                    headline in its own red-bordered box below the
//                    title and add the auto-open ``traceback`` row.
// The third return value (``none``) covers the edge case where
// failure_details is empty entirely — no headline, no body.  Test names
// alone carry the load in that variant.
function _failureCardLayoutForCase(testCase) {
    const detailsText = String(testCase && testCase.failure_details || '');
    if (!detailsText.trim()) {
        return { variant: 'none', headlineMessage: '', tracebackBody: '' };
    }
    const { headlineMessage, tracebackBody } = _splitFailureDetails(detailsText);
    if (!headlineMessage && !tracebackBody) {
        return { variant: 'none', headlineMessage: '', tracebackBody: '' };
    }
    const variant = tracebackBody ? 'two-row' : 'inline';
    return { variant, headlineMessage, tracebackBody };
}

function _splitFailureDetails(text) {
    if (!text) return { headlineMessage: '', tracebackBody: '' };
    const lines = String(text).split('\n');
    if (lines.length === 0) return { headlineMessage: '', tracebackBody: '' };
    // First non-empty line is the headline; the rest is the body.
    let headlineIdx = -1;
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim().length > 0) { headlineIdx = i; break; }
    }
    if (headlineIdx === -1) return { headlineMessage: '', tracebackBody: '' };
    const headlineMessage = lines[headlineIdx].trim();
    const tracebackBody = lines.slice(headlineIdx + 1).join('\n').trim();
    return { headlineMessage, tracebackBody };
}

function _formatDuration(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds)) return '';
    if (seconds === 0) return '0 ms';
    if (seconds >= 1) return `${seconds.toFixed(2)} s`;
    return `${Math.round(seconds * 1000)} ms`;
}

function _formatMs(ms) {
    if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
    return `${Math.round(ms)} ms`;
}
