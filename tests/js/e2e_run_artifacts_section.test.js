// JS-vm tests for the first-class "Run artifacts" drill-down section (#6593).
//
// Collected e2e_run_artifacts must be discoverable directly from the failed
// run panel (not buried in the Diagnostics disclosure), and the per-state
// diagnostic note must distinguish collected / globs-matched-nothing /
// not-configured.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadModule() {
    const context = {
        console,
        window: { dashboardData: { agents: [] } },
        escapeHtml: (value) => String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;'),
        escapeAttr: (value) => String(value).replace(/"/g, '&quot;'),
        formatTimestamp: () => '',
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '../../src/issue_orchestrator/static/js/dashboard/e2e_run_view.js'),
        'utf8',
    );
    vm.runInContext(source, context, { filename: 'e2e_run_view.js' });
    return context;
}

function _collectedRunData() {
    return {
        run: { id: 37, log_path: '/results/run_37/run-e2e-suite.log' },
        reports: [
            { kind: 'junit_xml', label: 'JUnit XML: tixmeup-e2e-smoke.xml', path: '/results/run_37/tixmeup-e2e-smoke.xml' },
        ],
        artifacts: [
            { kind: 'junit_xml', label: 'JUnit XML: tixmeup-e2e-smoke.xml', path: '/results/run_37/tixmeup-e2e-smoke.xml' },
            { kind: 'text_artifact', label: 'Text Artifact: run-e2e-suite.log', path: '/results/run_37/run-e2e-suite.log' },
            { kind: 'text_artifact', label: 'Text Artifact: compose-services.log', path: '/results/run_37/compose-services.log' },
        ],
        artifact_diagnostic: { state: 'collected', collected_count: 3, configured_glob_count: 3 },
    };
}

test('renders a drill-down button per collected artifact', () => {
    const ctx = loadModule();
    const html = ctx._renderRunArtifactsSection(_collectedRunData());

    assert.match(html, /class="e2e-run-artifacts"/);
    assert.match(html, /Run artifacts/);
    // Raw output + junit report + two extra text artifacts.
    assert.match(html, /Raw Output/);
    assert.match(html, /JUnit XML: tixmeup-e2e-smoke.xml/);
    assert.match(html, /Text Artifact: run-e2e-suite.log/);
    assert.match(html, /Text Artifact: compose-services.log/);
    // Every button carries the typed artifact-path contract, not an inline
    // file:// onclick.
    const buttons = [...html.matchAll(/data-artifact-path="([^"]+)"/g)];
    assert.ok(buttons.length >= 4);
    assert.match(html, /openE2EArtifactFromButton\(this\)/);
    // "collected" state renders no diagnostic note (the buttons speak for it).
    assert.doesNotMatch(html, /e2e-artifacts-note/);
});

test('button dispatch opens the artifact path via openPath', () => {
    const ctx = loadModule();
    const opened = [];
    ctx.window.openPath = (p) => opened.push(p);
    ctx.openE2EArtifactFromButton({ dataset: { artifactPath: '/results/run_37/run-e2e-suite.log' } });
    assert.deepEqual(opened, ['/results/run_37/run-e2e-suite.log']);
});

test('globs_matched_nothing renders an explanatory warn note but still shows raw output', () => {
    const ctx = loadModule();
    const html = ctx._renderRunArtifactsSection({
        run: { id: 5, log_path: '/results/run_5/run.log' },
        artifacts: [],
        reports: [],
        artifact_diagnostic: { state: 'globs_matched_nothing', collected_count: 0, configured_glob_count: 2 },
    });
    assert.match(html, /e2e-artifacts-note-warn/);
    assert.match(html, /none matched files in this run/);
    assert.match(html, /2 artifact globs are configured/);
    // Raw output is always available.
    assert.match(html, /Raw Output/);
});

test('not_configured renders an info note pointing at the config keys', () => {
    const ctx = loadModule();
    const html = ctx._renderRunArtifactsSection({
        run: { id: 6, log_path: '/results/run_6/run.log' },
        artifacts: [],
        reports: [],
        artifact_diagnostic: { state: 'not_configured', collected_count: 0, configured_glob_count: 0 },
    });
    assert.match(html, /e2e-artifacts-note-info/);
    assert.match(html, /No artifact globs are configured/);
    assert.match(html, /e2e.artifact_paths/);
});

test('missing artifact_diagnostic degrades to no note, still renders raw output', () => {
    const ctx = loadModule();
    const html = ctx._renderRunArtifactsSection({
        run: { id: 7, log_path: '/results/run_7/run.log' },
        artifacts: [],
        reports: [],
    });
    assert.doesNotMatch(html, /e2e-artifacts-note/);
    assert.match(html, /Raw Output/);
});

test('_artifactDiagnostic rejects unknown states', () => {
    const ctx = loadModule();
    assert.strictEqual(ctx._artifactDiagnostic({ artifact_diagnostic: { state: 'bogus' } }), null);
    assert.strictEqual(ctx._artifactDiagnostic({}), null);
    const ok = ctx._artifactDiagnostic({ artifact_diagnostic: { state: 'collected', collected_count: 2, configured_glob_count: 1 } });
    assert.strictEqual(ok.state, 'collected');
    assert.strictEqual(ok.collected_count, 2);
});
