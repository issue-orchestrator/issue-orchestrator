// Generated browser runtime validators (issue #6337).
//
// These tests are the contract's teeth in the browser.  They run against
// the REAL generated ``ui-contracts.validators.js`` — not a fixture —
// so a schema change that weakens a rule shows up here.
//
// The rejection cases matter more than the acceptance ones: this layer
// exists so a malformed payload cannot reach a feature handler, and each
// case below is a shape a hand-written check historically got wrong
// (coerced "88" to 88, treated `true` as 1, let an unknown field
// through, accepted run_id 0 because the guard only tested truthiness).

const test = require('node:test');
const assert = require('node:assert');

const validators = require('../../src/issue_orchestrator/static/js/ui-contracts.validators.js');

const COMMAND = 'TimelineCommandPayload';
const LIFECYCLE_COMMAND = 'LifecycleCommandPayload';

function openE2ERun(overrides = {}) {
    return { kind: 'open_e2e_run', label: 'Open E2E Run', run_id: 88, ...overrides };
}

function assertRejects(schemaName, value, pattern) {
    const result = validators.validate(schemaName, value);
    assert.strictEqual(result.ok, false, `expected rejection, got ok for ${JSON.stringify(value)}`);
    assert.strictEqual(result.value, null, 'a rejected payload must not be returned to callers');
    assert.match(result.errors.join(' | '), pattern);
    return result;
}

// ── registry ────────────────────────────────────────────────────────

test('registry exposes the OpenAPI components as browser schemas', () => {
    const names = validators.schemaNames();
    assert.ok(names.includes(COMMAND));
    assert.ok(names.includes('RecentE2ERunsPayload'));
    assert.ok(names.includes('DashboardViewModelPayload'));
    assert.ok(validators.hasSchema(COMMAND));
    assert.strictEqual(validators.hasSchema('NotAContract'), false);
});

test('an unknown schema name throws — it is a code bug, not wire data', () => {
    assert.throws(
        () => validators.validate('NotAContract', {}),
        /Unknown UI contract schema: NotAContract/,
    );
});

// ── acceptance ──────────────────────────────────────────────────────

test('a valid command is accepted and returned unchanged', () => {
    const payload = openE2ERun();
    const result = validators.validate(COMMAND, payload);
    assert.strictEqual(result.ok, true);
    assert.deepEqual(result.errors, []);
    assert.strictEqual(result.value, payload, 'validate returns the payload itself, not a copy');
});

test('optional properties may be present, absent, or explicitly null', () => {
    assert.strictEqual(validators.validate(COMMAND, openE2ERun({ expand_run_details: true })).ok, true);
    assert.strictEqual(validators.validate(COMMAND, openE2ERun()).ok, true);
    // Parity with the generated Pydantic model, where a non-required
    // field renders as ``T | None = None`` and accepts an explicit null.
    assert.strictEqual(
        validators.validate(COMMAND, {
            kind: 'open_session_recording',
            label: 'Session',
            issue_number: 7,
            run_dir: '/r',
            round_index: null,
            session_role: null,
        }).ok,
        true,
    );
});

test('each discriminated command variant validates against its own required fields', () => {
    const valid = [
        { kind: 'open_issue_timeline', label: 'T', issue_number: 7, scope_kind: 'dashboard' },
        { kind: 'open_completion_record', label: 'C', path: '/cr.json' },
        { kind: 'switch_e2e_timeline_view', label: 'V', run_id: 1, view: 'ops' },
        { kind: 'show_event_details', label: 'E', event_ref: 'evt-1' },
    ];
    for (const payload of valid) {
        const result = validators.validate(COMMAND, payload);
        assert.strictEqual(result.ok, true, `${payload.kind}: ${result.errors.join('; ')}`);
    }
});

test('the lifecycle boundary accepts timeline and dialog command families', () => {
    assert.strictEqual(validators.validate(LIFECYCLE_COMMAND, openE2ERun()).ok, true);
    assert.strictEqual(
        validators.validate(LIFECYCLE_COMMAND, {
            kind: 'open_path',
            label: 'Open Session Dir',
            path: '/tmp/run',
        }).ok,
        true,
    );
});

test('enforces string patterns from the canonical schema', () => {
    const digest = 'a'.repeat(64);
    assert.strictEqual(
        validators.validate('CompletionIntakeReceiptPayload', {
            entry_id: digest,
            content_sha256: digest,
        }).ok,
        true,
    );
    assertRejects(
        'CompletionIntakeReceiptPayload',
        { entry_id: 'not-a-digest', content_sha256: digest },
        /entry_id: does not match pattern/,
    );
});

// ── rejection: the cases feature code must never see ────────────────

test('rejects an unknown kind', () => {
    assertRejects(COMMAND, openE2ERun({ kind: 'not_a_command' }), /no contract variant matches string "not_a_command"/);
});

test('rejects a missing required field', () => {
    const payload = openE2ERun();
    delete payload.label;
    assertRejects(COMMAND, payload, /label: required property is missing/);
});

test('rejects a forbidden extra field', () => {
    assertRejects(COMMAND, openE2ERun({ smuggled: 'x' }), /smuggled: unexpected property is not allowed/);
});

test('rejects an invalid enum value', () => {
    assertRejects(
        COMMAND,
        { kind: 'switch_e2e_timeline_view', label: 'V', run_id: 1, view: 'nope' },
        /view: expected one of \["user","ops","debug","raw"\]/,
    );
});

test('rejects a string id — no coercion of "88" to 88', () => {
    assertRejects(COMMAND, openE2ERun({ run_id: '88' }), /run_id: expected integer, got string "88"/);
});

test('rejects a boolean id — no coercion of true to 1', () => {
    assertRejects(COMMAND, openE2ERun({ run_id: true }), /run_id: expected integer, got boolean true/);
});

test('rejects a non-positive id where the schema sets minimum: 1', () => {
    assertRejects(COMMAND, openE2ERun({ run_id: 0 }), /run_id: expected >= 1, got 0/);
    assertRejects(COMMAND, openE2ERun({ run_id: -5 }), /run_id: expected >= 1, got -5/);
});

test('rejects a fractional id where the schema says integer', () => {
    assertRejects(COMMAND, openE2ERun({ run_id: 1.5 }), /run_id: expected integer, got number 1.5/);
});

test('rejects a non-object payload', () => {
    assertRejects(COMMAND, null, /matches no contract variant, got null/);
    assertRejects(COMMAND, 'open_e2e_run', /matches no contract variant, got string/);
    assertRejects(COMMAND, [openE2ERun()], /matches no contract variant, got array/);
});

test('reports every violation in one pass, not just the first', () => {
    const result = validators.validate(COMMAND, { kind: 'open_e2e_run', run_id: 0, extra: 1 });
    assert.strictEqual(result.ok, false);
    const joined = result.errors.join(' | ');
    assert.match(joined, /label: required property is missing/);
    assert.match(joined, /run_id: expected >= 1/);
    assert.match(joined, /extra: unexpected property/);
});

// ── nesting ─────────────────────────────────────────────────────────

test('validates nested arrays of $ref items and reports the offending index', () => {
    const ok = validators.validate('RecentE2ERunsPayload', { runs: [] });
    assert.strictEqual(ok.ok, true);
    assertRejects(
        'RecentE2ERunsPayload',
        { runs: [{ kind: 'bogus' }] },
        /runs\[0\]/,
    );
    assertRejects('RecentE2ERunsPayload', {}, /runs: required property is missing/);
    assertRejects('RecentE2ERunsPayload', { runs: {} }, /runs: expected array/);
});

test('free-form objects (additionalProperties: true) accept arbitrary keys', () => {
    // ``IssueItemPayload`` deliberately permits source-specific fields,
    // so the validator must not invent a closed shape for it.
    const result = validators.validate('IssueItemPayload', {
        show_stale_badge: false,
        anything: 1,
        nested: { x: 'y' },
    });
    assert.strictEqual(result.ok, true);
});
