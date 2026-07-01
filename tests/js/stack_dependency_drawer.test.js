// JS-vm tests for the issue-detail drawer stack dependency section.
//
// Slices only the stack render functions out of issue_detail_drawer.js (the
// full file registers document listeners at load) and drives them with a
// minimal fake document, asserting the disclosure section shows producer-
// provided gate/edge state with text-first, non-colour-only status.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DRAWER_JS = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard/issue_detail_drawer.js',
);

function _extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const after = source.indexOf('\n}\n', start);
    if (after < 0) throw new Error(`function close not found: ${signaturePrefix}`);
    return source.slice(start, after + 3);
}

function _escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function fakeNode() {
    return { style: { display: '' }, innerHTML: '', textContent: '' };
}

function loadStackRenderer() {
    const source = fs.readFileSync(DRAWER_JS, 'utf8');
    const slice = [
        _extractFunction(source, 'function _stackGateItemHtml'),
        _extractFunction(source, 'function _stackEdgeItemHtml'),
        _extractFunction(source, 'function renderIssueDetailStack'),
    ].join('\n');
    const nodes = {
        issueDetailStack: fakeNode(),
        issueDetailStackBody: fakeNode(),
        issueDetailStackSummary: fakeNode(),
    };
    const context = {
        escapeHtml: _escapeHtml,
        document: { getElementById: (id) => nodes[id] || null },
    };
    vm.createContext(context);
    vm.runInContext(slice, context, { filename: 'issue_detail_drawer.js (stack slice)' });
    return { context, nodes };
}

function stackDependency(overrides = {}) {
    return {
        issue_number: 20,
        mode: 'stack',
        has_stack_edges: true,
        gates: [
            { gate: 'work', open: true, reason_codes: [], reasons: [] },
            { gate: 'publish', open: false, reason_codes: ['predecessor_not_merged'], reasons: ['the predecessor has not merged yet'] },
        ],
        predecessors: [{ ref: '#10', mode: 'stack', state: 'unsatisfied', problem: null }],
        successors: [{ issue_number: 30, ref: '#30', mode: 'stack' }],
        blocked_gates: ['publish', 'merge'],
        blocked_reason_codes: ['predecessor_not_merged'],
        stale: false,
        stale_reason_codes: [],
        stack_base_branch: 'feat/base',
        ...overrides,
    };
}

test('section is hidden when there is no stack dependency data', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({ stack_dependency: null });
    assert.strictEqual(nodes.issueDetailStack.style.display, 'none');
    assert.strictEqual(nodes.issueDetailStackBody.innerHTML, '');
});

test('section renders gates with text status, predecessors and successors', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({ stack_dependency: stackDependency() });
    assert.strictEqual(nodes.issueDetailStack.style.display, '');
    const html = nodes.issueDetailStackBody.innerHTML;
    // Gate state is text ("open"/"blocked"), not colour-only.
    assert.match(html, /stack-gate--open/);
    assert.match(html, /stack-gate--blocked/);
    assert.match(html, /<span class="stack-gate-state">blocked<\/span>/);
    // Human reason phrase for the blocked gate.
    assert.match(html, /the predecessor has not merged yet/);
    // Chain context: depends-on and stacked-behind.
    assert.match(html, /Depends on/);
    assert.match(html, /#10/);
    assert.match(html, /Stacked behind this/);
    assert.match(html, /#30/);
    // Stack base branch is shown.
    assert.match(html, /feat\/base/);
    // Decorative gate icons are aria-hidden.
    assert.match(html, /<span class="stack-gate-icon" aria-hidden="true">/);
});

test('summary label switches to plain dependencies when not a stack', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({
            mode: 'normal',
            has_stack_edges: false,
            successors: [],
            stack_base_branch: null,
        }),
    });
    assert.strictEqual(nodes.issueDetailStackSummary.textContent, 'Dependencies & gates');
});

test('stale slice shows a text stale note with reason codes', () => {
    const { context, nodes } = loadStackRenderer();
    context.renderIssueDetailStack({
        stack_dependency: stackDependency({ stale: true, stale_reason_codes: ['predecessor_branch_advanced'] }),
    });
    const html = nodes.issueDetailStackBody.innerHTML;
    assert.match(html, /stack-stale/);
    assert.match(html, /needs rebuilding/);
    assert.match(html, /predecessor_branch_advanced/);
    // The stale icon is decorative; the word "Stale" carries the meaning.
    assert.match(html, /aria-hidden="true">🔄/);
    assert.match(html, /Stale/);
});
