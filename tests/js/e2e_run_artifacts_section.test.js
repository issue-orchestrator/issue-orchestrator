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

// Faithful shape of the ``/api/e2e-run-detail`` payload: the backend builds
// ``artifacts`` with the synthetic raw-output entry first, then the collected
// ``e2e_run_artifacts`` rows, and derives ``reports`` as a *subset* of
// ``artifacts`` (report-class kinds). So the JUnit XML appears in BOTH arrays,
// and the raw-output log — when a glob re-collects it — appears both as the
// synthetic ``raw_log`` and as a ``text_artifact`` sharing ``log_path``. After
// JSON parsing these are distinct object instances, so dedupe must key on
// ``path``, not object identity (issue #6593 F1).
const JUNIT_PATH = '/results/run_37/tixmeup-e2e-smoke.xml';
const RAW_LOG_PATH = '/results/run_37/run-e2e-suite.log';
const COMPOSE_LOG_PATH = '/results/run_37/compose-services.log';

function _collectedRunData() {
    return {
        run: { id: 37, log_path: RAW_LOG_PATH },
        reports: [
            { kind: 'junit_xml', label: 'JUnit XML: tixmeup-e2e-smoke.xml', path: JUNIT_PATH },
        ],
        artifacts: [
            { kind: 'raw_log', label: 'Raw Output', path: RAW_LOG_PATH },
            { kind: 'junit_xml', label: 'JUnit XML: tixmeup-e2e-smoke.xml', path: JUNIT_PATH },
            { kind: 'text_artifact', label: 'Text Artifact: run-e2e-suite.log', path: RAW_LOG_PATH },
            { kind: 'text_artifact', label: 'Text Artifact: compose-services.log', path: COMPOSE_LOG_PATH },
        ],
        artifact_diagnostic: { state: 'collected', collected_count: 3, configured_glob_count: 3 },
    };
}

test('renders exactly one drill-down button per unique artifact path', () => {
    const ctx = loadModule();
    const html = ctx._renderRunArtifactsSection(_collectedRunData());

    assert.match(html, /class="e2e-run-artifacts"/);
    assert.match(html, /Run artifacts/);
    // Raw output + junit report + one extra text artifact — three unique files.
    assert.match(html, /Raw Output/);
    assert.match(html, /JUnit XML: tixmeup-e2e-smoke.xml/);
    assert.match(html, /Text Artifact: compose-services.log/);

    // Dedupe by path: the JUnit XML (report + artifact) and the raw-output log
    // (raw_log synthetic + text_artifact) must each render exactly once. A
    // regression to identity-based filtering re-inflates these counts.
    const paths = [...html.matchAll(/data-artifact-path="([^"]+)"/g)].map((m) => m[1]);
    assert.strictEqual(paths.length, 3);
    assert.deepEqual([...paths].sort(), [COMPOSE_LOG_PATH, RAW_LOG_PATH, JUNIT_PATH].sort());
    assert.strictEqual(paths.filter((p) => p === JUNIT_PATH).length, 1);
    assert.strictEqual(paths.filter((p) => p === RAW_LOG_PATH).length, 1);

    // The count chip reflects unique files, not the inflated raw array length.
    assert.match(html, /class="e2e-run-artifacts-count">3 files</);

    assert.match(html, /openE2EArtifactFromButton\(this\)/);
    // "collected" state renders no diagnostic note (the buttons speak for it).
    assert.doesNotMatch(html, /e2e-artifacts-note/);
});

test('persisted raw_log row with a distinct path renders as its own drill-down', () => {
    // A collected ``e2e_run_artifacts`` row can carry ``kind: "raw_log"`` with a
    // path different from ``run.log_path`` (e.g. a per-service raw log). The
    // backend keys dedupe on ``(kind, path)`` and emits it, so the UI must not
    // suppress it by kind — only path-based dedupe applies (issue #6593 F1).
    const ctx = loadModule();
    const PERSISTED_RAW_PATH = '/results/run_37/agent-runner.log';
    const html = ctx._renderRunArtifactsSection({
        run: { id: 37, log_path: RAW_LOG_PATH },
        reports: [],
        artifacts: [
            { kind: 'raw_log', label: 'Raw Output', path: RAW_LOG_PATH },
            { kind: 'raw_log', label: 'Raw Log: agent-runner.log', path: PERSISTED_RAW_PATH },
        ],
        artifact_diagnostic: { state: 'collected', collected_count: 2, configured_glob_count: 1 },
    });

    // Synthetic ``run.log_path`` still owns the ``Raw Output`` label (paths match
    // the first persisted row, so dedupe keeps the synthetic one).
    assert.match(html, /Raw Output/);
    assert.match(html, /Raw Log: agent-runner.log/);

    const paths = [...html.matchAll(/data-artifact-path="([^"]+)"/g)].map((m) => m[1]);
    assert.deepEqual([...paths].sort(), [PERSISTED_RAW_PATH, RAW_LOG_PATH].sort());
    // The distinct persisted raw log renders exactly once — not suppressed.
    assert.strictEqual(paths.filter((p) => p === PERSISTED_RAW_PATH).length, 1);
    assert.strictEqual(paths.filter((p) => p === RAW_LOG_PATH).length, 1);
    assert.match(html, /class="e2e-run-artifacts-count">2 files</);
});

test('persisted raw_log renders even when the run has no log_path', () => {
    // With no synthetic ``run.log_path``, the only raw log is the persisted DB
    // row. A kind-based skip would leave the section with zero raw output; the
    // path-based dedupe lets it through as a first-class drill-down.
    const ctx = loadModule();
    const PERSISTED_RAW_PATH = '/results/run_41/run-e2e-suite.log';
    const html = ctx._renderRunArtifactsSection({
        run: { id: 41 },
        reports: [],
        artifacts: [
            { kind: 'raw_log', label: 'Raw Output', path: PERSISTED_RAW_PATH },
        ],
        artifact_diagnostic: { state: 'collected', collected_count: 1, configured_glob_count: 1 },
    });

    assert.match(html, /Raw Output/);
    const paths = [...html.matchAll(/data-artifact-path="([^"]+)"/g)].map((m) => m[1]);
    assert.deepEqual(paths, [PERSISTED_RAW_PATH]);
    assert.match(html, /class="e2e-run-artifacts-count">1 file</);
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
