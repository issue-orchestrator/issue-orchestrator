# JS-vm Tests (`tests/js/`)

This directory holds the **preferred middle layer** for testing dashboard JS: load a `.js` file into a `node:vm` context, call its functions, assert on the returned strings or stubbed-DOM mutations.

**Cost: <5 ms per test.** Use this layer instead of Playwright (~7 s/test) for anything that doesn't genuinely need a real browser. See `tests/AGENTS.md` for the full pyramid.

## When to add a test here

Any new clickable surface, dispatcher, render function, or fetch call in `static/js/dashboard/*.js`. The bar is "could this break and have nothing catch it before Playwright/production?"

Specifically:
- **Click dispatch** — every `data-action` / `data-lifecycle-command` / `data-artifact-path` branch
- **Render shape** — what HTML a function returns for each meaningful input combination
- **Fetch behavior** — URL, payload, success path, error path, empty path
- **DOM mutations** — toggle state, caret + aria mirror, classList changes

## Pattern

```js
const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function loadModule(overrides = {}) {
    const calls = [];
    const preLoadStubs = {
        // window-scoped helpers the module CALLS but doesn't define.
        // Stubbed BEFORE script load.
        showToast: (msg, sev) => calls.push(['toast', msg, sev]),
        fetch: async () => ({ ok: true, json: async () => ({}) }),
    };
    const context = { ...preLoadStubs, ...overrides };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(__dirname, '../../src/.../my_module.js'), 'utf8'),
        context,
    );
    // Functions DEFINED in the module overwrite anything we put in the
    // context. Re-apply observability stubs AFTER script load.
    Object.assign(context, {
        myDefinedFunction: (arg) => calls.push(['myDefinedFunction', arg]),
    }, overrides);
    return { context, calls };
}

test('button click dispatches the right handler', () => {
    const { context, calls } = loadModule();
    context.runMyActionFromButton({ dataset: { action: 'foo', id: '42' } });
    assert.deepEqual(calls, [['handleFoo', '42']]);
});

test('renderRow returns expected HTML for failed test', () => {
    const { context } = loadModule();
    const html = context.renderRow({ outcome: 'failed', name: 'test_x' });
    assert.match(html, /class="status failed"/);
    assert.match(html, /test_x/);
});
```

## Gotchas

1. **Cross-realm prototypes.** Use `node:assert` (non-strict), not `node:assert/strict`. `vm.runInContext` creates objects whose `Object.prototype` differs from the test runner's, and `deepStrictEqual` rejects them even when shape-equal.

2. **Module-defined globals.** If the module under test contains `function foo() {...}`, that declaration is hoisted into the vm context and overwrites any `foo` you set pre-load. Apply observability stubs for module-defined functions in a post-load `Object.assign`. (Example: `tests/js/e2e_run_view_actions.test.js` does this for `copyTestErrorFromRun`, `closeE2EIssue`, etc.)

3. **No jsdom.** The project deliberately has no `node_modules` for tests — keep stubs hand-rolled and minimal. For functions that walk the DOM (`closest`, `querySelectorAll`), build per-test mock nodes with only the surface the function-under-test touches.

4. **`escapeHtml` / `escapeAttr` live in another file.** Provide minimal entity-encoding shims in your harness; don't load the whole bundle.

## Running

```bash
node --test tests/js/<file>.test.js
```

Whole-suite runs are wired into the project's test target.
