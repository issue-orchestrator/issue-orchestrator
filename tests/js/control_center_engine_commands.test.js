// Control Center pause/resume surface the engine's failure cause (#8222).
//
// The Control Center answers a failed engine command with
// ``{"error": "passthrough_failed", "detail": <why>}``. Before #8222 the
// toast dropped the body and said only "Failed to pause repository engine".
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const uiContractJson = require('../../src/issue_orchestrator/static/js/ui_contract_json.js');

function loadControlCenter(fetchImpl) {
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/control_center.js'),
        'utf8',
    ).split("document.addEventListener('DOMContentLoaded'")[0];
    const toasts = [];
    const requests = [];
    const context = {
        URL,
        console,
        localStorage: { getItem: () => null, setItem() {} },
        window: {
            addEventListener() {},
            matchMedia: () => ({ addEventListener() {}, matches: false }),
            setTimeout() {},
            uiContractJson,
        },
        document: { addEventListener() {} },
        fetch: async (url, init) => {
            requests.push({ url, init });
            return fetchImpl(url, init);
        },
        setInterval() {},
        setTimeout() {},
        clearTimeout() {},
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.showToast = (message, kind) => toasts.push({ message, kind });
    context.loadRepos = async () => {};
    return { context, toasts, requests };
}

function failure(status, body) {
    return { ok: false, status, text: async () => JSON.stringify(body) };
}

for (const [verb, fn] of [['pause', 'pauseRepo'], ['resume', 'resumeRepo']]) {
    test(`${fn} toasts the engine's failure detail, not a bare failure`, async () => {
        const detail = `Engine did not answer ${verb} at http://127.0.0.1:8081 within 120s (ReadTimeout).`;
        const { context, toasts, requests } = loadControlCenter(
            async () => failure(504, { error: 'passthrough_failed', failure: 'no_answer', detail }),
        );

        await context[fn]('/repo');

        assert.strictEqual(requests[0].url, `/control/orchestrator/${verb}`);
        assert.deepStrictEqual(JSON.parse(requests[0].init.body), { repo_root: '/repo' });
        assert.strictEqual(toasts.length, 1);
        assert.strictEqual(toasts[0].kind, 'error');
        assert.strictEqual(toasts[0].message, `Failed to ${verb} repository engine: ${detail}`);
    });

    test(`${fn} still names the HTTP status when the body carries no cause`, async () => {
        const { context, toasts } = loadControlCenter(
            async () => ({ ok: false, status: 502, text: async () => '<html>bad gateway</html>' }),
        );

        await context[fn]('/repo');

        assert.strictEqual(toasts[0].message, `Failed to ${verb} repository engine: HTTP 502`);
    });
}
