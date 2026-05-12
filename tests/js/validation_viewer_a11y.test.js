// JS-vm tests for canonical validation viewer accessibility
// (issue #6310 follow-up, Phase D).
//
// The viewer's accessibility story is two-stage:
//
//   1. Render-time: bake ARIA roles into the HTML so a screen reader
//      sees a real tree the moment the DOM is mounted.
//      ``role="tree"`` on .cvv-root, ``role="treeitem"`` on every
//      .cvv-row, ``role="group"`` on every .cvv-row-body, with
//      ``aria-expanded`` reflecting the <details> open state.
//
//   2. Post-mount enhancer (live DOM): aria-level / aria-setsize /
//      aria-posinset + roving tabindex + delegated keyboard nav.
//
// These JS-vm tests cover the render-time invariants on raw HTML
// strings.  The post-mount enhancer needs a real DOM (focus, keyboard
// dispatch, toggle events) and is covered by Playwright tests in
// tests/e2e_web/test_validation_viewer_a11y.py.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadViewer() {
    const baseStubs = {
        console,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        escapeAttr: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
    };
    const context = { ...baseStubs };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/validation_viewer.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'validation_viewer.js' });
    return context;
}

function _samplePayload() {
    return {
        status: 'failed',
        junit_cases: [
            {
                case_id: 'a',
                display_name: 'test_alpha',
                suite_name: 'tests/test_a.py',
                outcome: 'failed',
                failure_details: 'AssertionError\n  at frame 1',
                system_out: 'before',
                system_err: 'spooky',
                extras: [],
            },
            {
                case_id: 'b',
                display_name: 'test_beta',
                suite_name: 'tests/test_b.py',
                outcome: 'passed',
                duration_seconds: 0.003,
                extras: [],
            },
            {
                case_id: 'c',
                display_name: 'test_gamma',
                suite_name: 'tests/test_c.py',
                outcome: 'skipped',
                extras: [],
            },
        ],
        stdout_excerpt: ['line a'],
        stderr_excerpt: ['line b'],
    };
}

// ── Render-time ARIA roles ──────────────────────────────────────────────────

test('a11y: viewer root has role="tree" with aria-orientation=vertical and a label', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    assert.match(html, /<div class="cvv-root"[^>]*role="tree"/);
    assert.match(html, /aria-orientation="vertical"/);
    assert.match(html, /aria-label="Validation results"/);
});

test('a11y: every cvv-row has role="treeitem" with aria-expanded set', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    const detailsOpens = html.match(/<details[^>]*class="[^"]*cvv-row[^"]*"[^>]*>/g) || [];
    assert.ok(detailsOpens.length >= 5, `expected several cvv-row treeitems, got ${detailsOpens.length}`);
    for (const tag of detailsOpens) {
        assert.match(tag, /role="treeitem"/, `treeitem role missing on: ${tag}`);
        assert.match(tag, /aria-expanded="(true|false)"/, `aria-expanded missing on: ${tag}`);
    }
});

test('a11y: every <details> row including traceback and stderr-on-error is COLLAPSED by default', () => {
    // Predictable-collapse rule (see issue #6322): nothing in the
    // canonical viewer auto-opens.  Previous design auto-opened the
    // traceback row (two-row variant) and stderr-on-error.  Both
    // now start closed.  The user clicks ``traceback ▸`` /
    // ``stderr ▸`` to drill in.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'x', display_name: 'fixture error', outcome: 'error',
            failure_details: 'TypeError\n  File "x.py", line 1',
            system_err: 'fixture stderr',
            extras: [],
        }],
    });
    const tracebackTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">traceback<\/span>)/);
    assert.ok(tracebackTag, 'traceback <details> tag not found');
    assert.match(tracebackTag[0], /aria-expanded="false"/,
        'traceback row must NOT auto-open');
    assert.doesNotMatch(tracebackTag[0], /\bopen\b/,
        'traceback row must NOT carry the open attribute');

    const stderrTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">stderr<\/span>)/);
    assert.ok(stderrTag, 'stderr <details> tag not found');
    assert.match(stderrTag[0], /aria-expanded="false"/,
        'stderr row must NOT auto-open even when outcome is error');
    assert.doesNotMatch(stderrTag[0], /\bopen\b/,
        'stderr row must NOT carry the open attribute');
});

test('a11y: collapsed rows render with aria-expanded="false" and no open attribute', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    // Pull out the Run stdout / Run stderr <details> opening tags and
    // assert they carry aria-expanded="false" + don't have the ``open``
    // boolean attribute.
    const runStdoutTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">Run stdout<\/span>)/);
    assert.ok(runStdoutTag, `Run stdout <details> tag not found in: ${html.slice(0, 200)}…`);
    assert.match(runStdoutTag[0], /aria-expanded="false"/);
    assert.doesNotMatch(runStdoutTag[0], /\bopen\b/);

    const runStderrTag = html.match(/<details[^>]*"cvv-row"[^>]*>(?=<summary[^<]*<span[^>]*>[^<]*<\/span><span class="cvv-title">Run stderr<\/span>)/);
    assert.ok(runStderrTag, 'Run stderr <details> tag not found');
    assert.match(runStderrTag[0], /aria-expanded="false"/);
    assert.doesNotMatch(runStderrTag[0], /\bopen\b/);
});

test('a11y: every cvv-row-body has role="group"', () => {
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer(_samplePayload());
    const bodies = html.match(/<div class="cvv-row-body"[^>]*>/g) || [];
    assert.ok(bodies.length >= 1, 'expected at least one row body');
    for (const body of bodies) {
        assert.match(body, /role="group"/, `role=group missing on: ${body}`);
    }
});

test('a11y: browse-by-file expander aria-expanded reflects the open attribute', () => {
    // When there are no failures, the browse expander auto-opens — its
    // aria-expanded must read "true".  When there ARE failures, it
    // stays closed and aria-expanded="false".
    const ctx = loadViewer();
    const passedOnly = ctx.renderCanonicalValidationViewer({
        status: 'passed',
        junit_cases: [{ case_id: 'a', display_name: 'a', outcome: 'passed', suite_name: 'tests/test_a.py', extras: [] }],
    });
    assert.match(passedOnly, /class="cvv-row cvv-row-browse"[^>]*aria-expanded="true"[^>]* open/);

    const mixed = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [
            { case_id: 'a', display_name: 'a', outcome: 'failed', failure_details: 'x', extras: [] },
            { case_id: 'b', display_name: 'b', outcome: 'passed', extras: [] },
        ],
    });
    assert.match(mixed, /class="cvv-row cvv-row-browse"[^>]*aria-expanded="false"/);
});

test('a11y: enhancer is exported as a symbol on the viewer module', () => {
    // Hosts (modal, drawer, E2E view) call
    // ``enhanceCanonicalValidationViewerAccessibility(root)`` after
    // mounting the HTML.  Missing the export would silently disable
    // keyboard nav.
    const ctx = loadViewer();
    assert.strictEqual(
        typeof ctx.enhanceCanonicalValidationViewerAccessibility,
        'function',
        'enhancer must be available on the viewer module',
    );
});

// ── Triage card role="group" (reviewer Blocker 1 on PR #6316) ──────────────

test('a11y: failed triage card is a treeitem; its body carries role="group"', () => {
    // Phase D redesign (issue #6322): the triage card is now a
    // ``<details role="treeitem">`` closed by default.  Its body
    // ``<div class="cvv-triage-body" role="group">`` is the owning
    // group for the children (traceback / stdout / stderr leaf rows).
    //
    // Why both roles matter:
    //   - The card itself is a treeitem so it participates in the
    //     ARIA tree at the top level (aria-level=1).
    //   - The body's role="group" gives the leaf rows a proper
    //     parent group for ``aria-setsize``/``aria-posinset``
    //     enumeration — without it, ``_treeitemSiblings`` would
    //     resolve all the way up to ``.cvv-root[role=tree]`` and
    //     leak counts from unrelated top-level rows.
    const ctx = loadViewer();
    const html = ctx.renderCanonicalValidationViewer({
        status: 'failed',
        junit_cases: [{
            case_id: 'a', display_name: 'test_alpha', outcome: 'failed',
            failure_details: 'AssertionError: bad\n  at frame 1',
            system_out: 'out', system_err: 'err',
            extras: [],
        }],
    });
    // Card itself: <details role="treeitem">
    const cardTag = html.match(/<details class="cvv-triage-card[^"]*"[^>]*>/);
    assert.ok(cardTag, 'triage card should render as <details>');
    assert.match(cardTag[0], /role="treeitem"/, 'card must be a treeitem');
    assert.match(cardTag[0], /aria-expanded="false"/, 'card must start collapsed');
    // Body: <div class="cvv-triage-body" role="group">
    const bodyTag = html.match(/<div class="cvv-triage-body"[^>]*>/);
    assert.ok(bodyTag, 'triage card body should render');
    assert.match(bodyTag[0], /role="group"/, 'body must own the group for child leaf rows');
});

// ── Key → command translation (pure, no DOM needed) ────────────────────────

test('keyboard cmd: ArrowDown / ArrowUp map to next / prev regardless of details state', () => {
    const ctx = loadViewer();
    for (const isDetails of [true, false]) {
        for (const isOpen of [true, false]) {
            assert.strictEqual(ctx._treeCommandForKey('ArrowDown', { isDetails, isOpen }), 'next');
            assert.strictEqual(ctx._treeCommandForKey('ArrowUp', { isDetails, isOpen }), 'prev');
        }
    }
});

test('keyboard cmd: ArrowRight expands a collapsed details, focuses first child otherwise', () => {
    const ctx = loadViewer();
    assert.strictEqual(ctx._treeCommandForKey('ArrowRight', { isDetails: true, isOpen: false }), 'expand');
    assert.strictEqual(ctx._treeCommandForKey('ArrowRight', { isDetails: true, isOpen: true }), 'focus-first-child');
    assert.strictEqual(ctx._treeCommandForKey('ArrowRight', { isDetails: false, isOpen: false }), 'focus-first-child');
});

test('keyboard cmd: ArrowLeft collapses an open details, focuses parent otherwise', () => {
    const ctx = loadViewer();
    assert.strictEqual(ctx._treeCommandForKey('ArrowLeft', { isDetails: true, isOpen: true }), 'collapse');
    assert.strictEqual(ctx._treeCommandForKey('ArrowLeft', { isDetails: true, isOpen: false }), 'focus-parent');
    assert.strictEqual(ctx._treeCommandForKey('ArrowLeft', { isDetails: false, isOpen: false }), 'focus-parent');
});

test('keyboard cmd: Home / End map to first / last', () => {
    const ctx = loadViewer();
    assert.strictEqual(ctx._treeCommandForKey('Home', { isDetails: false, isOpen: false }), 'first');
    assert.strictEqual(ctx._treeCommandForKey('End', { isDetails: false, isOpen: false }), 'last');
});

test('keyboard cmd: Enter / Space toggle details, no-op otherwise', () => {
    const ctx = loadViewer();
    assert.strictEqual(ctx._treeCommandForKey('Enter', { isDetails: true, isOpen: false }), 'toggle');
    assert.strictEqual(ctx._treeCommandForKey(' ', { isDetails: true, isOpen: true }), 'toggle');
    assert.strictEqual(ctx._treeCommandForKey('Enter', { isDetails: false, isOpen: false }), null);
    assert.strictEqual(ctx._treeCommandForKey(' ', { isDetails: false, isOpen: false }), null);
});

test('keyboard cmd: unrelated keys return null (no preventDefault)', () => {
    const ctx = loadViewer();
    for (const key of ['Tab', 'Escape', 'a', 'PageDown']) {
        assert.strictEqual(ctx._treeCommandForKey(key, { isDetails: true, isOpen: true }), null);
    }
});

// ── Command executor with a fake-tree ops adapter ──────────────────────────
//
// The executor only talks to the DOM through ``ops``.  Tests pass a
// fake adapter backed by a tiny tree of plain-JS objects; the
// commands' results (focus movement, expand/collapse, roving tabindex)
// are observable on those objects.

function makeFakeTree() {
    // Build a small tree:
    //   root
    //     ├─ A (details, collapsed)   ← starts focused (tabIndex=0)
    //     │   ├─ A1 (details)
    //     │   └─ A2 (details)
    //     ├─ B (details, open)
    //     │   └─ B1 (details)
    //     └─ C (details, collapsed)
    function node(name, opts = {}) {
        return {
            name,
            tagName: opts.tagName || 'DETAILS',
            open: !!opts.open,
            tabIndex: -1,
            focused: false,
            children: [],
            parent: null,
            focus() { this.focused = true; },
        };
    }
    const A = node('A');
    const A1 = node('A1');
    const A2 = node('A2');
    const B = node('B', { open: true });
    const B1 = node('B1');
    const C = node('C');
    A.children = [A1, A2]; A1.parent = A; A2.parent = A;
    B.children = [B1]; B1.parent = B;
    const root = { name: 'root', children: [A, B, C] };
    A.parent = B.parent = C.parent = root;
    A.tabIndex = 0;  // initial focus / tab-stop
    A.focused = true;
    return { root, A, A1, A2, B, B1, C };
}

function makeFakeOps() {
    // The ops surface matches what ``_executeTreeCommand`` uses.  All
    // implementations operate on the fake-tree node objects above.
    function flatten(root) {
        // Pre-order traversal.
        const out = [];
        function walk(node) {
            for (const child of node.children) {
                out.push(child);
                walk(child);
            }
        }
        walk(root);
        return out;
    }
    function isVisible(node) {
        // Visible = every ancestor is open.
        let p = node.parent;
        while (p && p.children) {
            if (p.children && p.tagName === 'DETAILS' && !p.open) return false;
            p = p.parent;
        }
        return true;
    }
    function visibleList(root) {
        return flatten(root).filter(isVisible);
    }
    return {
        nextVisible: (item, root) => {
            const v = visibleList(root);
            const i = v.indexOf(item);
            return i >= 0 && i < v.length - 1 ? v[i + 1] : null;
        },
        prevVisible: (item, root) => {
            const v = visibleList(root);
            const i = v.indexOf(item);
            return i > 0 ? v[i - 1] : null;
        },
        firstVisible: (root) => visibleList(root)[0] || null,
        lastVisible: (root) => {
            const v = visibleList(root);
            return v.length > 0 ? v[v.length - 1] : null;
        },
        firstChild: (item) => item.children && item.children[0] || null,
        // Only return the parent if it's itself a treeitem (mirrors the
        // production adapter, which uses ``closest('[role="treeitem"]')``
        // and therefore skips non-treeitem ancestors like the tree
        // root or unrelated groups).  In the fake tree, the root is
        // not a treeitem (it has no ``tagName``).
        parent: (item) => item.parent && item.parent.tagName === 'DETAILS' ? item.parent : null,
        setOpen: (item, val) => { if (item.tagName === 'DETAILS') item.open = !!val; },
        getOpen: (item) => !!(item.tagName === 'DETAILS' && item.open),
        focusItem: (item, root) => {
            for (const n of flatten(root)) { n.tabIndex = -1; n.focused = false; }
            item.tabIndex = 0;
            item.focused = true;
        },
    };
}

test('executor: next moves focus to the following visible treeitem and updates the roving tabindex', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    // Start at A (tabIndex=0).  A is collapsed so its children A1/A2 aren't visible.
    // Next visible after A should be B.
    const ok = ctx._executeTreeCommand('next', tree.A, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.A.tabIndex, -1, 'A loses tab-stop');
    assert.strictEqual(tree.B.tabIndex, 0, 'B gains tab-stop');
    assert.strictEqual(tree.B.focused, true, 'B receives focus');
});

test('executor: next from the last visible item returns false (no movement)', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    ops.focusItem(tree.C, tree.root);
    const ok = ctx._executeTreeCommand('next', tree.C, tree.root, ops);
    assert.strictEqual(ok, false);
    assert.strictEqual(tree.C.focused, true, 'focus stays put');
});

test('executor: prev moves focus to the preceding visible treeitem', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    ops.focusItem(tree.B, tree.root);
    const ok = ctx._executeTreeCommand('prev', tree.B, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.A.focused, true);
});

test('executor: expand sets details.open without moving focus', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    assert.strictEqual(tree.A.open, false);
    const ok = ctx._executeTreeCommand('expand', tree.A, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.A.open, true);
    assert.strictEqual(tree.A.focused, true, 'focus does not change on expand');
});

test('executor: collapse clears details.open', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    const ok = ctx._executeTreeCommand('collapse', tree.B, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.B.open, false);
});

test('executor: focus-first-child moves focus to the first child treeitem', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    // B is open and has one child B1.
    ops.focusItem(tree.B, tree.root);
    const ok = ctx._executeTreeCommand('focus-first-child', tree.B, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.B1.focused, true);
});

test('executor: focus-first-child returns false when there is no child', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    ops.focusItem(tree.C, tree.root);
    const ok = ctx._executeTreeCommand('focus-first-child', tree.C, tree.root, ops);
    assert.strictEqual(ok, false);
});

test('executor: focus-parent moves focus to the parent treeitem', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    ops.focusItem(tree.B1, tree.root);
    const ok = ctx._executeTreeCommand('focus-parent', tree.B1, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.B.focused, true);
});

test('executor: focus-parent at the top of the tree returns false', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    const ok = ctx._executeTreeCommand('focus-parent', tree.A, tree.root, ops);
    assert.strictEqual(ok, false);
});

test('executor: first / last focus the boundary visible items', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    ops.focusItem(tree.B, tree.root);

    let ok = ctx._executeTreeCommand('first', tree.B, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.A.focused, true);

    ok = ctx._executeTreeCommand('last', tree.A, tree.root, ops);
    assert.strictEqual(ok, true);
    assert.strictEqual(tree.C.focused, true);
});

test('executor: toggle flips details.open', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    const wasOpen = tree.B.open;
    ctx._executeTreeCommand('toggle', tree.B, tree.root, ops);
    assert.strictEqual(tree.B.open, !wasOpen);
    ctx._executeTreeCommand('toggle', tree.B, tree.root, ops);
    assert.strictEqual(tree.B.open, wasOpen);
});

test('executor: invisible items are skipped by visible-traversal commands', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    // A collapsed → A1/A2 are NOT visible.
    // From A, ArrowDown → B (not A1).
    let cmd = ctx._treeCommandForKey('ArrowDown', { isDetails: true, isOpen: tree.A.open });
    assert.strictEqual(cmd, 'next');
    ctx._executeTreeCommand(cmd, tree.A, tree.root, ops);
    assert.strictEqual(tree.B.focused, true);
    assert.strictEqual(tree.A1.focused, false);
});

test('executor: roving tabindex always has exactly one tab-stop after any focus command', () => {
    const ctx = loadViewer();
    const tree = makeFakeTree();
    const ops = makeFakeOps();
    function countTabStops() {
        let n = 0;
        function walk(node) {
            if (node.tabIndex === 0) n++;
            for (const c of node.children || []) walk(c);
        }
        walk(tree.root);
        return n;
    }
    assert.strictEqual(countTabStops(), 1);
    ctx._executeTreeCommand('next', tree.A, tree.root, ops);
    assert.strictEqual(countTabStops(), 1, 'one tab-stop after next');
    ctx._executeTreeCommand('focus-first-child', tree.B, tree.root, ops);
    assert.strictEqual(countTabStops(), 1, 'one tab-stop after focus-first-child');
    ctx._executeTreeCommand('first', tree.B1, tree.root, ops);
    assert.strictEqual(countTabStops(), 1, 'one tab-stop after first');
});
