const test = require('node:test');
const assert = require('node:assert/strict');

const settingsSaveErrors = require('../../src/issue_orchestrator/static/js/settings_save_errors.js');

test('formatSaveErrorMessage includes validation error names and details', () => {
    const message = settingsSaveErrors.formatSaveErrorMessage({
        error: 'Validation failed',
        errors: [
            { name: 'worktree_base', detail: 'Path does not exist: /missing/worktrees' },
            { name: 'Token Scope', detail: 'Token is missing repo permission' },
        ],
    });

    assert.equal(
        message,
        'Validation failed:\n'
            + '- worktree_base: Path does not exist: /missing/worktrees\n'
            + '- Token Scope: Token is missing repo permission',
    );
});

test('formatSaveErrorMessage falls back when response body has no details', () => {
    assert.equal(
        settingsSaveErrors.formatSaveErrorMessage({}, 'Failed to save settings (HTTP 500)'),
        'Failed to save settings (HTTP 500)',
    );
});

test('formatErrorDetail skips blank entries and preserves detail-only entries', () => {
    assert.equal(settingsSaveErrors.formatErrorDetail({ name: '', detail: 'Invalid role' }), 'Invalid role');
    assert.equal(settingsSaveErrors.formatErrorDetail({ name: 'role', detail: '' }), 'role');
    assert.equal(settingsSaveErrors.formatErrorDetail({ name: ' ', detail: ' ' }), null);
});
