const test = require('node:test');
const assert = require('node:assert/strict');

const controls = require('../../src/issue_orchestrator/static/js/settings_form_controls.js');

// --- collectFieldValue: token dispatch over server-classified kinds --------

function stubControl(kind, props = {}) {
    return { dataset: { type: kind }, ...props };
}

test('collectFieldValue dispatches each supported kind to a typed value', () => {
    assert.equal(controls.collectFieldValue(stubControl('boolean', { checked: true })), true);
    assert.equal(controls.collectFieldValue(stubControl('enum', { value: 'surface' })), 'surface');
    assert.equal(controls.collectFieldValue(stubControl('integer', { value: '42' })), 42);
    assert.equal(controls.collectFieldValue(stubControl('number', { value: '1800.5' })), 1800.5);
    assert.equal(controls.collectFieldValue(stubControl('string', { value: 'hello' })), 'hello');
    assert.equal(controls.collectFieldValue(stubControl('optional_string', { value: '' })), null);
    assert.equal(controls.collectFieldValue(stubControl('optional_string', { value: 'x' })), 'x');
    // optional_integer: empty means unset (null), otherwise a parsed integer.
    assert.equal(controls.collectFieldValue(stubControl('optional_integer', { value: '' })), null);
    assert.equal(controls.collectFieldValue(stubControl('optional_integer', { value: '2' })), 2);
    assert.equal(controls.collectFieldValue(stubControl('optional_integer', { value: 'abc' })), null);
});

test('collectFieldValue throws on an unknown control kind (fail-fast)', () => {
    assert.throws(
        () => controls.collectFieldValue(stubControl('mystery')),
        /Unsupported settings control kind: mystery/,
    );
});

test('resetFieldValue throws on an unknown control kind (fail-fast)', () => {
    assert.throws(
        () => controls.resetFieldValue(stubControl('mystery'), 'v'),
        /Unsupported settings control kind: mystery/,
    );
});

test('resetFieldValue maps null/undefined back to empty string inputs', () => {
    const el = stubControl('optional_string', { value: 'old' });
    controls.resetFieldValue(el, null);
    assert.equal(el.value, '');
    controls.resetFieldValue(el, 'restored');
    assert.equal(el.value, 'restored');
});

// --- collectDictEntries: empty/duplicate keys block, never silently merge --

test('collectDictEntries builds an object from rows', () => {
    const { value, problems } = controls.collectDictEntries(
        [
            { key: 'agent:frontend', value: 'address' },
            { key: ' agent:backend ', value: 'ignore' },
        ],
        'Nit Policy By Agent',
    );
    assert.deepEqual(value, { 'agent:frontend': 'address', 'agent:backend': 'ignore' });
    assert.deepEqual(problems, []);
});

test('collectDictEntries reports empty keys instead of dropping rows', () => {
    const { value, problems } = controls.collectDictEntries(
        [{ key: '  ', value: 'surface' }],
        'Nit Policy By Agent',
    );
    assert.deepEqual(value, {});
    assert.equal(problems.length, 1);
    assert.match(problems[0], /row 1 has an empty key/);
});

test('collectDictEntries reports duplicate keys instead of last-wins', () => {
    const { value, problems } = controls.collectDictEntries(
        [
            { key: 'agent:frontend', value: 'address' },
            { key: 'agent:frontend', value: 'ignore' },
        ],
        'Nit Policy By Agent',
    );
    assert.deepEqual(value, { 'agent:frontend': 'address' });
    assert.equal(problems.length, 1);
    assert.match(problems[0], /duplicate key "agent:frontend"/);
});

// --- renderDictRowHtml: render shape and escaping ---------------------------

test('renderDictRowHtml renders key input, enum select with selection, remove button', () => {
    const html = controls.renderDictRowHtml(
        'agent:frontend',
        'address',
        ['ignore', 'surface', 'address'],
        'Nit Policy By Agent',
    );
    assert.match(html, /class="dict-row" role="group"/);
    assert.match(html, /value="agent:frontend"/);
    assert.match(html, /<option value="address" selected>address<\/option>/);
    assert.match(html, /<option value="ignore">ignore<\/option>/);
    assert.match(html, /class="btn btn-secondary dict-remove"/);
    assert.match(html, /aria-label="Nit Policy By Agent: key"/);
});

test('renderDictRowHtml escapes hostile keys', () => {
    const html = controls.renderDictRowHtml(
        '"><script>alert(1)</script>',
        'surface',
        ['ignore', 'surface', 'address'],
        'Nit Policy By Agent',
    );
    assert.ok(!html.includes('<script>alert(1)</script>'));
    assert.match(html, /&quot;&gt;&lt;script&gt;/);
});

test('escapeHtml escapes all dangerous characters', () => {
    assert.equal(
        controls.escapeHtml(`<a href="x" data-y='z'>&</a>`),
        '&lt;a href=&quot;x&quot; data-y=&#39;z&#39;&gt;&amp;&lt;/a&gt;',
    );
});

// --- collectForm: whole-form producer contract -------------------------------

test('collectForm posts every tab present in the document, not just edited ones', () => {
    // The settings form has no notion of a "dirty tab": collectForm gathers
    // every [data-tab][data-field] control in the document, so a save always
    // carries ALL tabs. The server-side field-granular patch plan
    // (settings_schema.build_save_plan) exists precisely because of this
    // producer contract -- if this test's shape ever narrowed to dirty-only,
    // the persistence policy would need to change with it.
    const nodes = [
        { dataset: { tab: 'concurrency', field: 'max_concurrent_sessions', type: 'integer' }, value: '3' },
        { dataset: { tab: 'advanced', field: 'sqlite_backup_enabled', type: 'boolean' }, checked: true },
        { dataset: { tab: 'goal_pilot', field: 'enabled', type: 'boolean' }, checked: false },
    ];
    const rootEl = { querySelectorAll: () => nodes };

    const { payload, problems } = controls.collectForm(rootEl);

    assert.deepEqual(problems, []);
    // Every tab with a control is present in the posted payload.
    assert.deepEqual(Object.keys(payload).sort(), ['advanced', 'concurrency', 'goal_pilot']);
    assert.equal(payload.concurrency.max_concurrent_sessions, 3);
    assert.equal(payload.advanced.sqlite_backup_enabled, true);
    assert.equal(payload.goal_pilot.enabled, false);
});
