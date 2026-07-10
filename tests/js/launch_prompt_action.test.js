// JS-vm tests for the context-menu launch-prompt action (#6588).
//
// The "View Agent Prompt" action for an active session opens the run-scoped
// launch prompt (manifest session_prompt_path) in a modal labelled as the
// launch prompt, instead of the static agent template. This exercises the
// standalone `openLaunchPromptDialog` helper from `session_dialogs.js` in
// isolation (the file has top-level side effects, so we extract the one
// function under test).

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function extractFunction(source, signaturePrefix) {
    const start = source.indexOf(signaturePrefix);
    if (start < 0) throw new Error(`function not found: ${signaturePrefix}`);
    const after = source.indexOf('\n}\n', start);
    if (after < 0) throw new Error(`function close not found: ${signaturePrefix}`);
    return source.slice(start, after + 3);
}

function loadDialog(overrides = {}) {
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/session_dialogs.js'),
        'utf8',
    );
    const fnSource = extractFunction(source, 'async function openLaunchPromptDialog');
    const calls = { fetch: [], modal: [], toast: [] };
    const context = {
        URLSearchParams,
        escapeHtml: (v) => String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
        openModal: (title, html) => calls.modal.push({ title, html }),
        showToast: (msg, sev) => calls.toast.push({ msg, sev }),
        fetch: async (url) => {
            calls.fetch.push(url);
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    prompt_path: '/runs/rework-454/session-prompt.txt',
                    label: 'Launch prompt',
                    content: 'Resolve the merge conflict in PR #469',
                }),
            };
        },
        ...overrides,
    };
    vm.createContext(context);
    vm.runInContext(fnSource, context);
    return { context, calls };
}

test('openLaunchPromptDialog fetches the run-scoped prompt and opens a launch-prompt modal', async () => {
    const { context, calls } = loadDialog();

    await context.openLaunchPromptDialog(454, '/runs/rework-454');

    assert.equal(calls.fetch.length, 1);
    assert.match(calls.fetch[0], /^\/api\/session\/prompt\/454\?/);
    assert.match(calls.fetch[0], /run_dir=/);
    assert.equal(calls.modal.length, 1);
    assert.equal(calls.modal[0].title, 'Launch Prompt #454');
    assert.match(calls.modal[0].html, /Launch prompt: \/runs\/rework-454\/session-prompt\.txt/);
    assert.match(calls.modal[0].html, /Resolve the merge conflict in PR #469/);
    assert.equal(calls.toast.length, 0);
});

test('openLaunchPromptDialog surfaces an error toast when the prompt is unavailable', async () => {
    const { context, calls } = loadDialog({
        fetch: async () => ({
            ok: false,
            status: 404,
            json: async () => ({ error: 'No run-scoped prompt artifact found for this session' }),
        }),
    });

    await context.openLaunchPromptDialog(454, '/runs/none');

    assert.equal(calls.modal.length, 0);
    assert.equal(calls.toast.length, 1);
    assert.equal(calls.toast[0].sev, 'error');
    assert.match(calls.toast[0].msg, /No run-scoped prompt artifact/);
});
