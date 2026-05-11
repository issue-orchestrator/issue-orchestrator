// Canonical JUnit / validation viewer + Phase-0 plugin registry.
//
// This module renders the rich validation-results body that the
// validation dialog (and, in Phase B, the per-issue drawer's
// "Validation" cycle event; and Phase C, the E2E run view) shares.
//
// Inputs: a ``ValidationFailureDialogPayload`` (despite the historical
// name, the payload covers both passed and failed runs).  The key
// per-test data is ``data.junit_cases`` — a list of
// ``JUnitCasePayload`` objects.
//
// Each junit case may carry ``case.extras: [{namespace, payload}, ...]``
// (Phase-0 plugin slot).  For each extra, we look up a renderer
// registered for the namespace and embed the result below the case's
// per-test detail.  Unknown namespaces silently skip — the design
// boundary is "the viewer is generic; orchestrator-specific concepts
// live in plugin modules registered by issue-orchestrator's dashboard".
// See ``docs/journeys/validation-viewer-redesign.md`` for the why.
//
// Phase-0 scope (this commit) — what's here:
//   * registry: ``registerValidationPlugin(namespace, renderer)``,
//     ``renderPluginExtras(case)``.
//   * canonical viewer: ``renderCanonicalValidationViewer(data)``.
// What's NOT here (deliberate Phase-0 limits — see redesign doc):
//   * stdout marker protocol (we own the parser; case.extras is fine)
//   * plugin manifest / dynamic loading
//   * version negotiation in namespaces
//   * fallback "unknown plugin" UI

// ── Plugin registry ─────────────────────────────────────────────────────────

const _validationPluginRegistry = Object.create(null);

function registerValidationPlugin(namespace, renderer) {
    if (typeof namespace !== 'string' || !namespace) {
        throw new Error('registerValidationPlugin: namespace must be a non-empty string');
    }
    if (typeof renderer !== 'function') {
        throw new Error(`registerValidationPlugin: renderer for ${namespace} must be a function`);
    }
    _validationPluginRegistry[namespace] = renderer;
}

function getValidationPlugin(namespace) {
    return _validationPluginRegistry[namespace] || null;
}

function renderPluginExtras(testCase) {
    const extras = Array.isArray(testCase && testCase.extras) ? testCase.extras : [];
    if (extras.length === 0) return '';
    const parts = [];
    for (const extra of extras) {
        if (!extra || typeof extra !== 'object') continue;
        const renderer = _validationPluginRegistry[extra.namespace];
        if (!renderer) continue;  // unknown namespace: silently skip
        try {
            const html = renderer(extra.payload, testCase);
            if (typeof html === 'string' && html) parts.push(html);
        } catch (err) {
            // A misbehaving plugin must not crash the whole viewer.
            // Show a single-line error inline so the bug is visible to
            // the user instead of vanishing.
            const msg = err && err.message ? err.message : String(err);
            parts.push(`<div class="diag-validation-plugin-error" data-namespace="${escapeAttr(extra.namespace)}">Plugin <code>${escapeHtml(extra.namespace)}</code> failed to render: ${escapeHtml(msg)}</div>`);
        }
    }
    return parts.join('');
}

// Test-only hook so JS-vm tests can reset the registry between cases
// without depending on module reload semantics.
function _resetValidationPluginRegistryForTests() {
    for (const k of Object.keys(_validationPluginRegistry)) {
        delete _validationPluginRegistry[k];
    }
}

// ── Canonical viewer ────────────────────────────────────────────────────────

function renderCanonicalValidationViewer(data) {
    // Tolerate partial payloads: production always sends the full shape
    // (the route validates against ``ValidationFailureDialogPayload``),
    // but JS-vm tests + the per-event embed in Phase B may pass slimmer
    // objects.  Default arrays to empty.
    const cases = Array.isArray(data && data.junit_cases) ? data.junit_cases : [];
    const stdoutExcerpt = Array.isArray(data && data.stdout_excerpt) ? data.stdout_excerpt : [];
    const stderrExcerpt = Array.isArray(data && data.stderr_excerpt) ? data.stderr_excerpt : [];
    const actionSections = Array.isArray(data && data.action_sections) ? data.action_sections : [];
    const status = (data && data.status === 'passed') ? 'passed' : 'failed';

    const failureCases = cases.filter((c) => c && (c.outcome === 'failed' || c.outcome === 'error'));
    const otherCases = cases.filter((c) => c && c.outcome !== 'failed' && c.outcome !== 'error');

    let html = '<div class="cvv-root" data-cvv-status="' + escapeAttr(status) + '">';

    // Triage: failed/errored tests as cards at the top, auto-expanded.
    if (failureCases.length > 0) {
        html += '<section class="cvv-triage">';
        for (let i = 0; i < failureCases.length; i++) {
            html += _renderTriageCard(failureCases[i], `cvv-fail-${i}`);
        }
        html += '</section>';
    }

    // Browse-by-file for non-failed cases (passed/skipped).  Single
    // top-level expander; clicking opens the file list.  Each file
    // expands to test rows; each test expands to its stdout / duration /
    // sparkline.
    if (otherCases.length > 0) {
        const passedCount = otherCases.filter((c) => c.outcome === 'passed').length;
        const skippedCount = otherCases.filter((c) => c.outcome === 'skipped').length;
        const summaryParts = [];
        if (passedCount > 0) summaryParts.push(`${passedCount} passed`);
        if (skippedCount > 0) summaryParts.push(`${skippedCount} skipped`);
        const summary = (failureCases.length > 0 ? '+ ' : '') + summaryParts.join(', ');
        html += '<section class="cvv-browse">';
        html += `<details class="cvv-row cvv-row-browse" ${failureCases.length === 0 ? 'open' : ''}>`;
        html += `<summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-passed">✓</span><span class="cvv-title">${escapeHtml(summary)}</span><span class="cvv-summary">browse by file</span></summary>`;
        html += '<div class="cvv-row-body">';
        html += _renderBrowseByFile(otherCases, 'cvv-browse');
        html += '</div></details>';
        html += '</section>';
    }

    // stdout/stderr excerpts — preserved from the historical dialog
    // shape for callers that still want them.  Per-test cases already
    // have their own stdout/stderr in the triage cards / per-test
    // expansion above, but the *run-level* excerpts are different (they
    // capture orchestrator-side stdout, not per-test).  Render them in
    // a collapsed footer so they don't compete with the test detail.
    if (stdoutExcerpt.length > 0 || stderrExcerpt.length > 0) {
        html += '<section class="cvv-run-output">';
        if (stdoutExcerpt.length > 0) {
            html += `<details class="cvv-row"><summary><span class="cvv-caret">▸</span><span class="cvv-title">Run stdout</span><span class="cvv-summary">${stdoutExcerpt.length} line${stdoutExcerpt.length === 1 ? '' : 's'}</span></summary>`;
            html += `<pre class="cvv-pre">${escapeHtml(stdoutExcerpt.join('\n'))}</pre>`;
            html += '</details>';
        }
        if (stderrExcerpt.length > 0) {
            html += `<details class="cvv-row" ${status === 'failed' ? '' : ''}><summary><span class="cvv-caret">▸</span><span class="cvv-title">Run stderr</span><span class="cvv-summary">${stderrExcerpt.length} line${stderrExcerpt.length === 1 ? '' : 's'}</span></summary>`;
            html += `<pre class="cvv-pre">${escapeHtml(stderrExcerpt.join('\n'))}</pre>`;
            html += '</details>';
        }
        html += '</section>';
    }

    // Validation artifacts (record / output / stderr / session evidence
    // / diagnostics) — historical action_sections, rendered as a
    // collapsed footer so the user's eye lands on tests first.
    if (actionSections.length > 0) {
        html += '<section class="cvv-artifacts">';
        html += '<details class="cvv-row"><summary><span class="cvv-caret">▸</span><span class="cvv-title">Validation artifacts</span><span class="cvv-summary">record · output · evidence</span></summary>';
        html += '<div class="cvv-row-body">';
        html += renderValidationFailureActionSections(actionSections);
        html += '</div></details>';
        html += '</section>';
    }

    html += '</div>';  // cvv-root
    return html;
}

// ── Triage card (one failed/errored test, auto-expanded) ────────────────────

function _renderTriageCard(testCase, idPrefix) {
    const outcome = testCase.outcome === 'error' ? 'error' : 'failed';
    const headlineKind = outcome === 'error' ? 'is-error' : 'is-failed';
    const displayName = String(testCase.display_name || testCase.case_id || '(unnamed test)');
    const suiteName = testCase.suite_name ? String(testCase.suite_name) : '';
    const duration = _formatDuration(testCase.duration_seconds);

    // Parse failure_details into a one-line headline + body.  JUnit
    // packs both into a single text blob; we split on the first newline.
    const { headlineMessage, tracebackBody } = _splitFailureDetails(testCase.failure_details || '');

    let html = `<div class="cvv-triage-card cvv-${outcome}">`;

    html += '<div class="cvv-triage-head">';
    html += `<span class="cvv-ico cvv-ico-${outcome}">${outcome === 'error' ? '⚠' : '✕'}</span>`;
    html += `<span class="cvv-triage-title">${escapeHtml(displayName)}</span>`;
    if (suiteName) html += `<span class="cvv-summary">${escapeHtml(suiteName)}</span>`;
    html += '</div>';

    if (headlineMessage) {
        html += `<div class="cvv-headline ${headlineKind}"><span class="cvv-headline-text">${escapeHtml(headlineMessage)}</span></div>`;
    }

    html += '<div class="cvv-badges">';
    html += `<span class="cvv-chip cvv-chip-${outcome}">${outcome === 'error' ? '⚠ Errored' : '✕ Failed'}</span>`;
    if (duration) html += `<span class="cvv-chip">${escapeHtml(duration)}</span>`;
    html += '</div>';

    if (tracebackBody) {
        html += `<details class="cvv-row" open><summary><span class="cvv-caret">▸</span><span class="cvv-title">traceback</span></summary>`;
        html += `<pre class="cvv-pre cvv-pre-fail">${escapeHtml(tracebackBody)}</pre>`;
        html += '</details>';
    }

    html += _renderTestSystemOutErr(testCase, idPrefix, outcome === 'error');

    // Plugin extras: render below the test detail, before any closing
    // actions row.  Currently no Phase-0 plugin renders into triage
    // cards (linked-issue lives on E2E tests in Phase C), but the slot
    // is here so the architecture is consistent.
    html += renderPluginExtras(testCase);

    html += '</div>';  // cvv-triage-card
    return html;
}

// ── Browse-by-file (passed + skipped) ───────────────────────────────────────

function _renderBrowseByFile(cases, idPrefix) {
    const byFile = new Map();
    for (const c of cases) {
        const key = String(c.suite_name || '(unknown file)');
        if (!byFile.has(key)) byFile.set(key, []);
        byFile.get(key).push(c);
    }
    const files = Array.from(byFile.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    if (files.length === 0) return '';
    let html = '';
    for (let i = 0; i < files.length; i++) {
        const [fileName, items] = files[i];
        const passCount = items.filter((c) => c.outcome === 'passed').length;
        const skipCount = items.filter((c) => c.outcome === 'skipped').length;
        const totalMs = items.reduce((s, c) => s + ((typeof c.duration_seconds === 'number') ? c.duration_seconds * 1000 : 0), 0);
        const statsParts = [];
        if (passCount > 0) statsParts.push(`${passCount} passed`);
        if (skipCount > 0) statsParts.push(`${skipCount} skipped`);
        if (totalMs > 0) statsParts.push(_formatMs(totalMs));
        const stats = statsParts.join(' · ');
        const base = fileName.split('/').pop();
        const dirPart = fileName.length > base.length ? fileName.slice(0, fileName.length - base.length - 1) : '';
        html += `<details class="cvv-row cvv-file"><summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-passed">✓</span><span class="cvv-title">${escapeHtml(base)}</span>${dirPart ? `<span class="cvv-summary">${escapeHtml(dirPart)}</span>` : ''}<span class="cvv-meta">${escapeHtml(stats)}</span></summary>`;
        html += '<div class="cvv-row-body">';
        for (let j = 0; j < items.length; j++) {
            html += _renderPassedTestRow(items[j], `${idPrefix}-f${i}-t${j}`);
        }
        html += '</div></details>';
    }
    return html;
}

function _renderPassedTestRow(testCase, idPrefix) {
    const displayName = String(testCase.display_name || testCase.case_id || '(unnamed test)');
    const duration = _formatDuration(testCase.duration_seconds);
    const outcome = testCase.outcome === 'skipped' ? 'skipped' : 'passed';
    const outcomeIcon = outcome === 'skipped' ? '–' : '✓';

    let html = `<details class="cvv-row cvv-test">`;
    html += `<summary><span class="cvv-caret">▸</span><span class="cvv-ico cvv-ico-${outcome}">${outcomeIcon}</span><span class="cvv-title">${escapeHtml(displayName)}</span>${duration ? `<span class="cvv-meta">${escapeHtml(duration)}</span>` : ''}</summary>`;
    html += '<div class="cvv-row-body">';
    html += '<div class="cvv-badges">';
    html += `<span class="cvv-chip cvv-chip-${outcome}">${outcome === 'skipped' ? '– Skipped' : '✓ Passed'}</span>`;
    if (duration) html += `<span class="cvv-chip">${escapeHtml(duration)}</span>`;
    html += '</div>';
    html += _renderTestSystemOutErr(testCase, idPrefix, false);
    html += renderPluginExtras(testCase);
    html += '</div></details>';
    return html;
}

function _renderTestSystemOutErr(testCase, idPrefix, errorOpenStderr) {
    let html = '';
    const stdout = testCase.system_out || '';
    const stderr = testCase.system_err || '';
    const stdoutLines = stdout ? stdout.split('\n').filter((l) => l.length > 0).length : 0;
    const stderrLines = stderr ? stderr.split('\n').filter((l) => l.length > 0).length : 0;

    html += `<details class="cvv-row"><summary><span class="cvv-caret">▸</span><span class="cvv-title">stdout</span><span class="cvv-summary">${stdoutLines === 0 ? 'empty' : `${stdoutLines} line${stdoutLines === 1 ? '' : 's'}`}</span></summary>`;
    html += stdout ? `<pre class="cvv-pre">${escapeHtml(stdout)}</pre>` : '<div class="cvv-empty">No stdout captured.</div>';
    html += '</details>';

    html += `<details class="cvv-row"${errorOpenStderr && stderr ? ' open' : ''}><summary><span class="cvv-caret">▸</span><span class="cvv-title">stderr</span><span class="cvv-summary">${stderrLines === 0 ? 'empty' : `${stderrLines} line${stderrLines === 1 ? '' : 's'}`}</span></summary>`;
    html += stderr ? `<pre class="cvv-pre">${escapeHtml(stderr)}</pre>` : '<div class="cvv-empty">No stderr captured.</div>';
    html += '</details>';

    return html;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function _splitFailureDetails(text) {
    if (!text) return { headlineMessage: '', tracebackBody: '' };
    const lines = String(text).split('\n');
    if (lines.length === 0) return { headlineMessage: '', tracebackBody: '' };
    // First non-empty line is the headline; the rest is the body.
    let headlineIdx = -1;
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim().length > 0) { headlineIdx = i; break; }
    }
    if (headlineIdx === -1) return { headlineMessage: '', tracebackBody: '' };
    const headlineMessage = lines[headlineIdx].trim();
    const tracebackBody = lines.slice(headlineIdx + 1).join('\n').trim();
    return { headlineMessage, tracebackBody };
}

function _formatDuration(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds)) return '';
    if (seconds === 0) return '0 ms';
    if (seconds >= 1) return `${seconds.toFixed(2)} s`;
    return `${Math.round(seconds * 1000)} ms`;
}

function _formatMs(ms) {
    if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
    return `${Math.round(ms)} ms`;
}
