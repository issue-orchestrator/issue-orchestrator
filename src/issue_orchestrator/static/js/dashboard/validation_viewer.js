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

    const quarantinedCases = cases.filter((c) => _isQuarantinedCase(c));
    const activeCases = cases.filter((c) => c && !_isQuarantinedCase(c));
    const failureCases = activeCases.filter((c) => c && (c.outcome === 'failed' || c.outcome === 'error'));
    const otherCases = activeCases.filter((c) => c && c.outcome !== 'failed' && c.outcome !== 'error');

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

    // Phase D redesign (issue #6322): outcome-grouped expanders.
    // Each outcome (Failed / Errored / Quarantined / Skipped / Passed) becomes a
    // top-level <details> closed by default; zero-count groups are
    // hidden entirely.  Severity order: Failed -> Errored -> Quarantined
    // -> Skipped -> Passed.  Replaces the old "always-visible triage cards +
    // collapsed browse-by-file" layout.
    //
    // Predictable-collapse: every group starts closed; nothing
    // auto-opens.  The group headers carry the counts AND the
    // affordance — no chips, no banner, no redundant summary.
    const failedCases = failureCases.filter((c) => c.outcome === 'failed');
    const erroredCases = failureCases.filter((c) => c.outcome === 'error');
    const skippedCases = otherCases.filter((c) => c.outcome === 'skipped');
    const passedCases = otherCases.filter((c) => c.outcome === 'passed');

    if (failedCases.length > 0) {
        html += _renderOutcomeGroup({
            outcome: 'failed',
            label: 'Failed',
            cases: failedCases,
            idPrefix: 'cvv-grp-failed',
        });
    }
    if (erroredCases.length > 0) {
        html += _renderOutcomeGroup({
            outcome: 'error',
            label: 'Errored',
            cases: erroredCases,
            idPrefix: 'cvv-grp-errored',
        });
    }
    if (quarantinedCases.length > 0) {
        html += _renderOutcomeGroup({
            outcome: 'quarantined',
            label: 'Quarantined',
            cases: quarantinedCases,
            idPrefix: 'cvv-grp-quarantined',
        });
    }
    if (skippedCases.length > 0) {
        html += _renderOutcomeGroup({
            outcome: 'skipped',
            label: 'Skipped',
            cases: skippedCases,
            idPrefix: 'cvv-grp-skipped',
        });
    }
    if (passedCases.length > 0) {
        html += _renderOutcomeGroup({
            outcome: 'passed',
            label: 'Passed',
            cases: passedCases,
            idPrefix: 'cvv-grp-passed',
        });
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

function _isQuarantinedCase(testCase) {
    return testCase && testCase.is_quarantined === true;
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
        if (target.open) _loadCapturedOutputOnDemand(target);
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

// ── Outcome-grouped expander (Phase D redesign — issue #6322) ──────────────
//
// Each outcome bucket (Failed / Errored / Skipped / Passed) renders
// as a top-level <details> closed by default.  Summary row:
//   caret + outcome icon + label + (count).
// Body: cases belonging to this outcome.  Failed/Errored render
// triage cards; Passed/Skipped render the browse-by-file tree
// (Skipped only — passed are included too when in the Passed
// group, but the browse-by-file helper is shared).
//
// Predictable-collapse: every group starts closed; nothing auto-
// opens at any level.

function _renderOutcomeGroup(opts) {
    const outcome = opts.outcome;
    const label = opts.label;
    const cases = opts.cases || [];
    const idPrefix = opts.idPrefix || `cvv-grp-${outcome}`;
    const iconChar = outcome === 'error' ? '⚠' : outcome === 'failed' ? '✕' : outcome === 'quarantined' ? 'Q' : outcome === 'skipped' ? '–' : '✓';
    const count = cases.length;

    let html = `<details class="cvv-row cvv-group cvv-group-${outcome}" role="treeitem" aria-expanded="false">`;
    html += '<summary>';
    html += '<span class="cvv-caret">▸</span>';
    html += `<span class="cvv-ico cvv-ico-${outcome}">${iconChar}</span>`;
    html += `<span class="cvv-title">${escapeHtml(label)}</span>`;
    html += `<span class="cvv-summary">(${count})</span>`;
    html += '</summary>';
    html += '<div class="cvv-row-body" role="group">';

    if (outcome === 'failed' || outcome === 'error') {
        // Failed and errored cases render as triage cards (each is
        // its own <details> expander further down — predictable-
        // collapse all the way through).
        for (let i = 0; i < cases.length; i++) {
            html += _renderTriageCard(cases[i], `${idPrefix}-${i}`);
        }
    } else if (outcome === 'quarantined') {
        const failureLike = cases.filter((c) => c && (c.outcome === 'failed' || c.outcome === 'error'));
        const nonFailureLike = cases.filter((c) => c && c.outcome !== 'failed' && c.outcome !== 'error');
        for (let i = 0; i < failureLike.length; i++) {
            html += _renderTriageCard(failureLike[i], `${idPrefix}-qf-${i}`);
        }
        if (nonFailureLike.length > 0) {
            html += _renderBrowseByFile(nonFailureLike, `${idPrefix}-qo`, 'quarantined');
        }
    } else {
        // Passed / Skipped cases share the browse-by-file tree.
        // The helper groups by suite file and renders each test as
        // a row.  Pass the group's outcome so the file row's icon
        // matches (skipped files get ``–`` not ``✓``).
        html += _renderBrowseByFile(cases, idPrefix, outcome);
    }

    html += '</div></details>';
    return html;
}

// ── Triage card (one failed/errored test, collapsed by default) ─────────────
//
// Phase D redesign (issue #6322): the triage card is now a
// ``<details>`` closed by default.  At landing the user sees only
// the summary row — caret + icon + test name + suite + inline 1-line
// error message + Copy-error icon.  Click to unfold the body: full
// headline box + chips + collapsed traceback / stdout / stderr leaf
// rows + plugin extras.
//
// Predictable-collapse: every clickable thing starts closed; never
// auto-opens.  A run with many failures lands as a scannable list
// of collapsed rows, not a wall of tracebacks.

function _renderTriageCard(testCase, idPrefix) {
    const outcome = testCase.outcome === 'error' ? 'error' : 'failed';
    const headlineKind = outcome === 'error' ? 'is-error' : 'is-failed';
    const displayName = String(testCase.display_name || testCase.case_id || '(unnamed test)');
    const suiteName = testCase.suite_name ? String(testCase.suite_name) : '';
    const duration = _formatDuration(testCase.duration_seconds);

    // Failure body: ``failure_details`` is the raw text; split it into
    // a 1-line headline (rendered in the summary row) and an optional
    // multi-line body (rendered as the traceback expander inside the
    // card body).  The old ``inline`` vs ``two-row`` layout variant is
    // gone — the card itself is the variant container now.
    const layout = _failureCardLayoutForCase(testCase);

    // Outer card is a ``<details role="treeitem">`` — it participates
    // in the ARIA tree at the top level of the canonical viewer.
    // Closed by default.
    let html = `<details class="cvv-triage-card cvv-${outcome}" role="treeitem" aria-expanded="false">`;

    // Summary row (the always-visible 1-liner).  Click toggles the
    // details.  Inside we render:
    //   caret + outcome icon + test name + (suite name) +
    //   inline 1-line error message + Copy-error icon
    html += '<summary class="cvv-triage-head">';
    html += '<span class="cvv-caret">▸</span>';
    html += `<span class="cvv-ico cvv-ico-${outcome}">${outcome === 'error' ? '⚠' : '✕'}</span>`;
    html += `<span class="cvv-triage-title">${escapeHtml(displayName)}</span>`;
    if (suiteName) html += `<span class="cvv-summary">${escapeHtml(suiteName)}</span>`;
    if (layout.headlineMessage) {
        html += `<span class="cvv-inline-headline ${headlineKind}" title="${escapeAttr(layout.headlineMessage)}">${escapeHtml(layout.headlineMessage)}</span>`;
    }
    // Copy-error icon.  Lives in the summary row so it's scan-
    // accessible without opening the card.  ``preventDefault`` and
    // ``stopPropagation`` are required: a click on a button inside
    // ``<summary>`` would otherwise toggle the parent ``<details>``.
    if (testCase.failure_details) {
        const copyPayload = escapeAttr(String(testCase.failure_details));
        html += `<button class="cvv-copy-icon" type="button" title="Copy error" aria-label="Copy error text to clipboard" data-cvv-copy-text="${copyPayload}" onclick="event.preventDefault(); event.stopPropagation(); _cvvCopyErrorFromButton(this);">⎘</button>`;
    }
    html += '</summary>';

    // Body of the expanded card.  ``role="group"`` so child treeitems
    // (traceback / stdout / stderr) have a proper parent group for
    // ARIA setsize/posinset enumeration.
    html += '<div class="cvv-triage-body" role="group">';

    // Full headline box.  The summary's inline-headline truncates with
    // ellipsis when long; the body's headline box shows the full
    // text so the user has it once they expand.
    if (layout.headlineMessage) {
        html += `<div class="cvv-headline ${headlineKind}"><span class="cvv-headline-text">${escapeHtml(layout.headlineMessage)}</span></div>`;
    }

    html += '<div class="cvv-badges">';
    html += `<span class="cvv-chip cvv-chip-${outcome}">${outcome === 'error' ? '⚠ Errored' : '✕ Failed'}</span>`;
    if (_isQuarantinedCase(testCase)) {
        html += '<span class="cvv-chip cvv-chip-quarantined">Q Quarantined</span>';
    }
    if (duration) html += `<span class="cvv-chip">${escapeHtml(duration)}</span>`;
    html += '</div>';

    // Traceback row (only when there's a multi-line body).  Closed
    // by default — same predictable-collapse rule.
    if (layout.tracebackBody) {
        html += `<details class="cvv-row" role="treeitem" aria-expanded="false"><summary><span class="cvv-caret">▸</span><span class="cvv-title">traceback</span></summary>`;
        html += `<pre class="cvv-pre cvv-pre-fail">${escapeHtml(layout.tracebackBody)}</pre>`;
        html += '</details>';
    }

    html += _renderTestSystemOutErr(testCase, idPrefix, false);

    // Plugin extras: render at the bottom of the expanded body.
    html += renderPluginExtras(testCase);

    html += '</div>';  // cvv-triage-body
    html += '</details>';  // cvv-triage-card
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
//
// Renders test cases grouped by their suite file.  Each file becomes
// a collapsed expander whose body lists the individual test rows.
//
// ``fileIconOutcome`` controls the icon on the FILE summary row.  When
// the caller is the Phase D outcome group (#6322), it passes the group's
// own outcome so a Skipped group's file rows show the skipped icon
// (``–``) rather than the passed icon (``✓``).  Default is ``passed``
// for back-compat with the older single-section browse caller.
function _renderBrowseByFile(cases, idPrefix, fileIconOutcome) {
    const fileOutcome = (fileIconOutcome === 'skipped' || fileIconOutcome === 'quarantined')
        ? fileIconOutcome
        : 'passed';
    const fileIcon = fileOutcome === 'quarantined' ? 'Q' : fileOutcome === 'skipped' ? '–' : '✓';

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
        html += `<details class="cvv-row cvv-file" role="treeitem" aria-expanded="false"><summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-${fileOutcome}">${fileIcon}</span><span class="cvv-title">${escapeHtml(base)}</span>${dirPart ? `<span class="cvv-summary">${escapeHtml(dirPart)}</span>` : ''}<span class="cvv-meta">${escapeHtml(stats)}</span></summary>`;
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
    if (_isQuarantinedCase(testCase)) {
        html += '<span class="cvv-chip cvv-chip-quarantined">Q Quarantined</span>';
    }
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

// Render stdout + stderr expander rows.  Both default COLLAPSED.
// (The third argument is retained for call-site compatibility but
// is now ignored — previous design auto-opened stderr for errored
// tests; the predictable rule is "never auto-open, user clicks to
// drill in".)
function _renderTestSystemOutErr(testCase, idPrefix, _errorOpenStderr) {
    let html = '';
    const stdout = testCase.system_out || '';
    const stderr = testCase.system_err || '';
    const capturedOutputUrl = _capturedOutputUrlFromCase(testCase);
    const capturedOutput = _capturedOutputAvailabilityFromCase(testCase);
    const hasCapturedOutputMetadata = _hasCapturedOutputAvailabilityMetadata(testCase);
    const caseId = String(testCase.case_id || idPrefix || '');

    html += _renderCapturedOutputRow(
        'stdout',
        stdout,
        capturedOutput.stdout_available ? capturedOutputUrl : '',
        caseId,
        {
            authoritativeUnavailable: hasCapturedOutputMetadata && !capturedOutput.stdout_available,
        },
    );
    html += _renderCapturedOutputRow(
        'stderr',
        stderr,
        capturedOutput.stderr_available ? capturedOutputUrl : '',
        caseId,
        {
            authoritativeUnavailable: hasCapturedOutputMetadata && !capturedOutput.stderr_available,
        },
    );

    return html;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function _capturedOutputUrlFromCase(testCase) {
    const value = testCase && testCase.captured_output_url;
    return typeof value === 'string' && value.trim() ? value.trim() : '';
}

function _capturedOutputAvailabilityFromCase(testCase) {
    const capturedOutput = testCase && testCase.captured_output && typeof testCase.captured_output === 'object'
        ? testCase.captured_output
        : {};
    return {
        stdout_available: capturedOutput.stdout_available === true,
        stderr_available: capturedOutput.stderr_available === true,
    };
}

function _hasCapturedOutputAvailabilityMetadata(testCase) {
    return !!(
        testCase
        && testCase.captured_output
        && typeof testCase.captured_output === 'object'
    );
}

function _renderCapturedOutputRow(channel, text, capturedOutputUrl, caseId, options) {
    const safeText = typeof text === 'string' ? text : '';
    const hasText = safeText.length > 0;
    const opts = options || {};
    if (!hasText && !capturedOutputUrl && opts.authoritativeUnavailable) {
        return (
            '<div class="cvv-row cvv-output-disabled" role="treeitem" aria-disabled="true">' +
            `<span class="cvv-caret" aria-hidden="true"></span><span class="cvv-title">${escapeHtml(channel)}</span>` +
            '<span class="cvv-summary">not captured</span>' +
            `<span class="cvv-empty cvv-output-body">No ${escapeHtml(channel)} captured.</span>` +
            '</div>'
        );
    }
    const summary = hasText ? _lineCountLabel(safeText) : capturedOutputUrl ? 'available' : 'empty';
    const lazyAttrs = capturedOutputUrl
        ? (
            ` data-cvv-output-url="${escapeAttr(capturedOutputUrl)}"` +
            ` data-cvv-output-channel="${escapeAttr(channel)}"` +
            ` data-cvv-output-case-id="${escapeAttr(caseId)}"` +
            ' data-cvv-output-state="idle"'
        )
        : '';
    const body = hasText
        ? `<pre class="cvv-pre cvv-output-body">${escapeHtml(safeText)}</pre>`
        : capturedOutputUrl
            ? `<div class="cvv-empty cvv-output-body" role="status" aria-live="polite">Captured ${escapeHtml(channel)} available.</div>`
            : `<div class="cvv-empty cvv-output-body">No ${escapeHtml(channel)} captured.</div>`;
    return (
        `<details class="cvv-row" role="treeitem" aria-expanded="false"${lazyAttrs}>` +
        `<summary><span class="cvv-caret">▸</span><span class="cvv-title">${escapeHtml(channel)}</span><span class="cvv-summary">${escapeHtml(summary)}</span></summary>` +
        body +
        '</details>'
    );
}

function _lineCountLabel(text) {
    const lines = String(text || '').split('\n').filter((line) => line.length > 0).length;
    if (lines === 0) return 'empty';
    return `${lines} line${lines === 1 ? '' : 's'}`;
}

function _loadCapturedOutputOnDemand(row) {
    if (!row || !row.dataset || !row.open) return;
    const url = String(row.dataset.cvvOutputUrl || '').trim();
    if (!url) return;
    if (row.dataset.cvvOutputState === 'loaded' || row.dataset.cvvOutputState === 'loading') return;
    if (typeof fetch !== 'function') {
        _markCapturedOutputRowsUnavailable(row, 'Captured output loader is unavailable.');
        return;
    }

    const rows = _capturedOutputRowsForUrl(row);
    for (const outputRow of rows) {
        outputRow.dataset.cvvOutputState = 'loading';
        _setCapturedOutputRowBody(
            outputRow,
            `Loading captured ${_capturedOutputChannel(outputRow)}...`,
            'loading',
        );
    }

    fetch(url)
        .then(async (response) => {
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                const message = payload && (payload.error || payload.detail)
                    ? String(payload.error || payload.detail)
                    : `HTTP ${response.status}`;
                throw new Error(message);
            }
            _applyCapturedOutputPayload(rows, payload);
        })
        .catch((error) => {
            const message = error && error.message
                ? `Captured output unavailable: ${error.message}`
                : 'Captured output unavailable.';
            _markCapturedOutputRowsUnavailable(row, message);
        });
}

function _capturedOutputRowsForUrl(row) {
    const url = String(row && row.dataset && row.dataset.cvvOutputUrl || '');
    const parent = row && row.parentElement;
    // Rendered stdout/stderr rows are siblings under one test row body; detached
    // unit-test fixtures can only update the row that initiated the fetch.
    const candidates = parent && typeof parent.querySelectorAll === 'function'
        ? Array.from(parent.querySelectorAll('details[data-cvv-output-url]'))
        : [row];
    return candidates.filter((candidate) => (
        candidate && candidate.dataset && candidate.dataset.cvvOutputUrl === url
    ));
}

function _applyCapturedOutputPayload(rows, payload) {
    for (const row of rows) {
        const channel = _capturedOutputChannel(row);
        const text = channel === 'stderr'
            ? _optionalOutputText(payload && payload.system_err)
            : _optionalOutputText(payload && payload.system_out);
        _setCapturedOutputRowLoaded(row, channel, text);
    }
}

function _optionalOutputText(value) {
    return typeof value === 'string' ? value : '';
}

function _setCapturedOutputRowLoaded(row, channel, text) {
    row.dataset.cvvOutputState = 'loaded';
    _setCapturedOutputSummary(row, _lineCountLabel(text));
    if (text) {
        _replaceCapturedOutputBody(
            row,
            `<pre class="cvv-pre cvv-output-body">${escapeHtml(text)}</pre>`,
        );
    } else {
        _replaceCapturedOutputBody(
            row,
            `<div class="cvv-empty cvv-output-body">No ${escapeHtml(channel)} captured.</div>`,
        );
    }
}

function _markCapturedOutputRowsUnavailable(row, message) {
    for (const outputRow of _capturedOutputRowsForUrl(row)) {
        outputRow.dataset.cvvOutputState = 'error';
        _setCapturedOutputSummary(outputRow, 'unavailable');
        _setCapturedOutputRowBody(outputRow, message, 'error');
    }
}

function _setCapturedOutputRowBody(row, message, state) {
    const roleAttrs = state === 'loading' ? ' role="status" aria-live="polite"' : '';
    _replaceCapturedOutputBody(
        row,
        `<div class="cvv-empty cvv-output-body"${roleAttrs}>${escapeHtml(message)}</div>`,
    );
    _setCapturedOutputSummary(row, state === 'loading' ? 'loading' : 'unavailable');
}

function _replaceCapturedOutputBody(row, html) {
    const body = _capturedOutputBody(row);
    if (body) {
        body.outerHTML = html;
    } else if (typeof row.insertAdjacentHTML === 'function') {
        row.insertAdjacentHTML('beforeend', html);
    }
}

function _capturedOutputBody(row) {
    const children = Array.from(row && row.children ? row.children : []);
    return children.find((child) => (
        child && child.classList && child.classList.contains('cvv-output-body')
    )) || null;
}

function _capturedOutputChannel(row) {
    const channel = String(row && row.dataset && row.dataset.cvvOutputChannel || '');
    return channel === 'stderr' ? 'stderr' : 'stdout';
}

function _setCapturedOutputSummary(row, text) {
    const summary = row && typeof row.querySelector === 'function'
        ? row.querySelector(':scope > summary .cvv-summary') || row.querySelector('.cvv-summary')
        : null;
    if (summary) summary.textContent = text;
}

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
