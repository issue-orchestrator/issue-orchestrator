// Pure tests for the shared hierarchical timeline row renderer.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS_DIR = path.join(
    __dirname,
    '../../src/issue_orchestrator/static/js/dashboard',
);

function _loadRenderer(extra = {}) {
    const ctx = {
        window: {},
        escapeHtml: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;'),
        escapeAttr: (value) => String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
        _renderLifecycleCommandAttr: (command) =>
            `data-lifecycle-command="${String(command.kind || '')}"`,
        renderTimelineEventActions: (actions) => {
            const buttons = (Array.isArray(actions) ? actions : [])
                .map((action) => `<button class="timeline-action-btn" data-action="${action.type}">${action.type}</button>`)
                .join('');
            return buttons ? `<div class="timeline-event-actions">${buttons}</div>` : '';
        },
        ...extra,
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(DASHBOARD_JS_DIR, 'hierarchical_timeline.js'), 'utf8'),
        ctx,
        { filename: 'hierarchical_timeline.js' },
    );
    return ctx;
}

test('renderHierarchicalTimelineNode owns details/summary/body, caret, and command toggle wiring', () => {
    const ctx = _loadRenderer();
    const html = ctx.renderHierarchicalTimelineNode({
        id: 'row-1',
        className: 'outer row',
        summaryClassName: 'row-summary',
        bodyClassName: 'row-body',
        bodyId: 'row-1-body',
        caretClassName: 'row-caret',
        role: 'listitem',
        open: true,
        attrs: { 'data-loaded': '', 'data-run-id': 88 },
        command: { kind: 'expand_e2e_run', run_id: 88 },
        summaryHtml: '<span>Run #88</span>',
        bodyHtml: '<div>Body</div>',
    });

    assert.match(html, /^<details class="outer row" id="row-1" open role="listitem"/);
    assert.match(html, /data-loaded=""/);
    assert.match(html, /data-run-id="88"/);
    assert.match(html, /data-lifecycle-command="expand_e2e_run"/);
    assert.match(html, /ontoggle="runLifecycleCommandFromToggle\(this\)"/);
    assert.match(html, /<summary class="row-summary"><span class="row-caret hierarchical-timeline-caret" aria-hidden="true"><\/span><span>Run #88<\/span><\/summary>/);
    assert.match(html, /<div class="row-body" id="row-1-body"><div>Body<\/div><\/div>/);
});

test('renderHierarchicalTimelineNode omits command hook when no command is supplied', () => {
    const ctx = _loadRenderer();
    const html = ctx.renderHierarchicalTimelineNode({
        className: 'journey-cycle',
        summaryClassName: 'journey-cycle-header',
        bodyClassName: 'journey-cycle-body',
        caretClassName: 'journey-cycle-toggle',
        summaryHtml: '<span>Cycle 1</span>',
        bodyHtml: '',
    });

    assert.match(html, /<details class="journey-cycle">/);
    assert.doesNotMatch(html, /data-lifecycle-command=/);
    assert.doesNotMatch(html, /ontoggle=/);
});

test('renderHierarchicalTimelineNode escapes attributes from caller-supplied node metadata', () => {
    const ctx = _loadRenderer();
    const html = ctx.renderHierarchicalTimelineNode({
        id: 'row"quoted',
        className: 'row',
        summaryClassName: 'summary',
        bodyClassName: 'body',
        bodyId: 'body<id>',
        attrs: { 'data-label': 'a "quoted" <label>' },
    });

    assert.match(html, /id="row&quot;quoted"/);
    assert.match(html, /id="body&lt;id&gt;"/);
    assert.match(html, /data-label="a &quot;quoted&quot; &lt;label&gt;"/);
});

test('host capability registry resolves late-bound services', () => {
    const ctx = _loadRenderer();
    const formatter = () => 'formatted';

    ctx.registerHierarchicalTimelineHostCapability('formatHeaderTimestamp', () => formatter);

    assert.strictEqual(ctx.getHierarchicalTimelineHostCapability('formatHeaderTimestamp'), formatter);
    assert.strictEqual(ctx.runHierarchicalTimelineHostCapability('formatHeaderTimestamp'), 'formatted');
    assert.strictEqual(ctx.getHierarchicalTimelineHostCapability('missing'), null);

    ctx._resetHierarchicalTimelineHostCapabilitiesForTests();

    assert.strictEqual(ctx.getHierarchicalTimelineHostCapability('formatHeaderTimestamp'), null);
});

test('outcome badge helper normalizes tone and fallback label', () => {
    const ctx = _loadRenderer();

    assert.deepEqual(
        ctx.readHierarchicalOutcomeBadge({ label: 'Failed', tone: 'failed' }),
        { label: 'Failed', tone: 'failed', toneClass: 'outcome-failed' },
    );
    assert.deepEqual(
        ctx.readHierarchicalOutcomeBadge({ label: '', tone: 'surprise' }, 'Unknown'),
        { label: 'Unknown', tone: 'neutral', toneClass: '' },
    );
    assert.strictEqual(ctx.hierarchicalToneGlyph('in_progress', { inProgress: '⟳' }), '⟳');
});
