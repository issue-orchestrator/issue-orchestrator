// JS-vm tests for the ``/api/issue-rows`` browser JSON boundary in
// ``dashboard/core.js`` (issue #6337).
//
// ``refreshIssueRows`` reconciles the rendered issue list against the
// endpoint's payload: rows whose ``issue_number`` is absent from the
// response are REMOVED from the DOM, and each surviving row's ``html``
// is written straight into the document.  Before #6337 the response was
// read with a bare ``res.json()`` and degraded via ``data.rows || []``,
// so a malformed body emptied the list — every issue vanished from the
// dashboard and it read as "no issues" rather than as a payload bug.
//
// These tests pin the fail-closed contract: a response that violates
// ``IssueRowsPayload`` must leave the previously rendered rows alone.

const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
    captureContractViolations,
    resetContractViolationReporter,
    uiContractJson,
} = require('./ui_contract_test_support.js');

const STATIC_JS_DIR = path.join(__dirname, '../../src/issue_orchestrator/static/js');
const CORE_JS = path.join(STATIC_JS_DIR, 'dashboard/core.js');

// core.js hard-requires these helpers at load time (it throws if any is
// missing — fail-fast by design).  Use the real modules rather than
// stubs: the fingerprint helper below actually runs during reconcile.
const issueRowState = require(path.join(STATIC_JS_DIR, 'issue_row_state.js'));
const expandedColumnState = require(path.join(STATIC_JS_DIR, 'expanded_column_state.js'));
const compactCardState = require(path.join(STATIC_JS_DIR, 'compact_card_state.js'));
const uiActionContract = require(path.join(STATIC_JS_DIR, 'ui_action_contract.js'));
const embeddedNav = require(path.join(STATIC_JS_DIR, 'embedded_nav.js'));

function _validIssueRows(rows) {
    return { rows, active_tab: 'all', count: rows.length };
}

function _issueRow(issueNumber) {
    return {
        issue_number: issueNumber,
        html: `<div class="issue-row-group" data-issue="${issueNumber}"></div>`,
    };
}

// A rendered row already in the list. ``removed`` is what the
// fail-closed assertion reads: reconcile calls ``remove()`` on any row
// missing from the payload.
function _renderedGroup(issueNumber) {
    return {
        dataset: { issue: String(issueNumber), rowFingerprint: 'stale' },
        removed: false,
        remove() { this.removed = true; },
        replaceWith() {},
        after() {},
        previousElementSibling: null,
    };
}

function _stubDocument(listEl) {
    const noopEl = {
        getAttribute: () => null,
        setAttribute: () => {},
        addEventListener: () => {},
        textContent: '',
        style: {},
        classList: { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
    };
    return {
        readyState: 'complete',
        documentElement: noopEl,
        body: noopEl,
        addEventListener: () => {},
        getElementById: (id) => (id === 'issueList' ? listEl : null),
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: () => ({
            dataset: {},
            set innerHTML(_v) {},
            get firstElementChild() { return null; },
        }),
    };
}

function _loadCore({ listEl, fetchImpl }) {
    const fetchCalls = [];
    const context = {
        console,
        // Web APIs, not ECMAScript built-ins — a fresh vm realm has
        // neither.
        URL,
        URLSearchParams,
        fetch: async (url) => {
            fetchCalls.push(url);
            return fetchImpl(url);
        },
        uiContractJson,
        issueRowState,
        expandedColumnState,
        compactCardState,
        uiActionContract,
        embeddedNav,
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        location: { search: '', origin: 'http://localhost', href: 'http://localhost/' },
        setTimeout: () => 0,
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
        requestAnimationFrame: (fn) => fn(),
        matchMedia: () => ({ matches: false, addEventListener: () => {} }),
        showToast: () => {},
        escapeHtml: (v) => String(v == null ? '' : v),
        escapeAttr: (v) => String(v == null ? '' : v),
        formatTimestamp: () => '',
        // Sibling-module helpers the reconcile tail calls
        // (``timestamp_formatting.js`` / ``controls_refresh.js``).
        // Only reached once a payload passes validation — the
        // fail-closed tests below return before this.
        formatDashboardTimestamps: () => {},
        initFlowLazyVisibleRefresh: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        document: _stubDocument(listEl),
    };
    context.window = context;
    context.self = context;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(CORE_JS, 'utf8'), context, { filename: 'core.js' });
    // core.js fetches /api/info on load; the tests below assert on what
    // the function under test requests, not on boot traffic.
    fetchCalls.length = 0;
    return { context, fetchCalls };
}

// The reconcile loop reaches the list through these queries only:
// the rendered row groups, the header it inserts after, and the
// empty-state element it shows/hides once rows are known.
function _stubList(groups) {
    const emptyState = { style: {}, innerHTML: '' };
    return {
        querySelectorAll: (sel) => (sel === '.issue-row-group[data-issue]' ? groups : []),
        querySelector: (sel) => (sel === '.empty-state' ? emptyState : null),
        appendChild: () => {},
    };
}

// Implements both ``text()`` and ``json()`` on purpose. The reader uses
// ``text()``, but the pre-#6337 code used ``json()``; supporting both
// means these tests fail against the OLD implementation for the real
// reason (it emptied the list) rather than passing because a stub
// method was missing.
function _response(body) {
    const raw = typeof body === 'string' ? body : JSON.stringify(body);
    return {
        ok: true,
        status: 200,
        url: '/api/issue-rows',
        text: async () => raw,
        json: async () => JSON.parse(raw),
    };
}

test.afterEach(() => resetContractViolationReporter());

test('issue rows refresh requests the contract-covered endpoint', async () => {
    const rendered = _renderedGroup(7);
    const { context, fetchCalls } = _loadCore({
        listEl: _stubList([rendered]),
        fetchImpl: async () => _response(_validIssueRows([_issueRow(7)])),
    });

    await context.refreshIssueRows({});

    assert.strictEqual(fetchCalls.length, 1);
    assert.ok(
        String(fetchCalls[0]).includes('/api/issue-rows'),
        `expected the issue-rows endpoint, got ${fetchCalls[0]}`,
    );
});

test('a contract-violating payload leaves the rendered rows in place', async () => {
    // ``count`` as a string is a contract violation the old
    // ``data.rows || []`` path never noticed.
    const rendered = _renderedGroup(7);
    const violations = captureContractViolations();
    const { context } = _loadCore({
        listEl: _stubList([rendered]),
        fetchImpl: async () => _response({ rows: [], active_tab: 'all', count: '0' }),
    });

    await context.refreshIssueRows({});

    assert.strictEqual(rendered.removed, false, 'a rejected payload must not remove rendered rows');
    assert.strictEqual(violations.length, 1, 'exactly one diagnostic per rejection');
    assert.strictEqual(violations[0].schemaName, 'IssueRowsPayload');
});

test('a payload missing the required rows field does not empty the list', async () => {
    const rendered = _renderedGroup(7);
    captureContractViolations();
    const { context } = _loadCore({
        listEl: _stubList([rendered]),
        fetchImpl: async () => _response({ active_tab: 'all', count: 0 }),
    });

    await context.refreshIssueRows({});

    assert.strictEqual(rendered.removed, false, 'a rejected payload must not remove rendered rows');
});

test('an HTML error page served with a 200 does not empty the list', async () => {
    const rendered = _renderedGroup(7);
    captureContractViolations();
    const { context } = _loadCore({
        listEl: _stubList([rendered]),
        fetchImpl: async () => _response('<html><body>Gateway Timeout</body></html>'),
    });

    await context.refreshIssueRows({});

    assert.strictEqual(rendered.removed, false, 'a rejected payload must not remove rendered rows');
});

test('a contract-valid payload still reconciles rows away that the server dropped', async () => {
    // The complement of fail-closed: a VALID payload must keep working.
    // Row 7 is absent from the response, so reconcile removes it.
    const rendered = _renderedGroup(7);
    const { context } = _loadCore({
        listEl: _stubList([rendered]),
        fetchImpl: async () => _response(_validIssueRows([])),
    });

    await context.refreshIssueRows({});

    assert.strictEqual(rendered.removed, true, 'a valid payload must still drive reconciliation');
});
