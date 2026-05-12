let unifiedRunData = null;  // Stores data for the current unified run view
const E2E_LABEL_OVERRIDES = Object.freeze({
    pytest: 'Pytest',
    command: 'Command',
    junit_xml: 'JUnit XML',
    html_report: 'HTML Report',
    json_report: 'JSON Report',
});

function _emptyE2EResultCategories() {
    return {
        untriaged: [],
        has_issue: [],
        flaky: [],
        fixed: [],
        passed: [],
        quarantined: [],
        skipped: [],
    };
}

async function _fetchE2ERunDetail(runId, view = 'user') {
    const response = await fetch(`/api/e2e-run-detail/${runId}?view=${encodeURIComponent(view)}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        const message = payload && typeof payload === 'object'
            ? payload.error || payload.detail || 'Failed to load run details'
            : 'Failed to load run details';
        throw new Error(String(message));
    }
    if (!payload || typeof payload !== 'object') {
        throw new Error('Run detail payload was not an object');
    }
    return payload;
}

/**
 * Show the unified run view for any E2E run.
 * This is the main entry point - called when clicking any run row.
 *
 * @param {number} runId - The E2E run ID to display
 */
async function showUnifiedRunView(runId, options) {
    options = options || {};
    const modal = document.getElementById('e2eDiagnosisModal');
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = modal.querySelector('.modal-header h2');

    modalTitle.textContent = `E2E Run #${runId}`;
    content.innerHTML = '<div class="loading-spinner">Loading run details...</div>';
    modal.classList.add('visible');
    // Phase D #6322: the E2E run view is no longer a dim-backdrop
    // modal — it's a full-page section.  Tag <body> so the dashboard
    // chrome hides behind it via CSS (overlays.css).  Cleared by
    // closeE2EDiagnosisModal().
    document.body.setAttribute('data-e2e-run-view-active', '1');

    try {
        unifiedRunData = await _fetchE2ERunDetail(runId, 'user');
        renderUnifiedRunView(unifiedRunData, runId, options);
    } catch (err) {
        content.innerHTML = `<div style="color: var(--danger); padding: 20px;">Failed to load run details: ${escapeHtml(err.message)}</div>`;
    }
}

function normalizeE2ETimelineData(timelineData) {
    timelineData = timelineData || {};
    const lifecycle = timelineData.lifecycle && typeof timelineData.lifecycle === 'object'
        ? timelineData.lifecycle
        : null;
    return {
        events: Array.isArray(timelineData.events) ? timelineData.events : [],
        phase_toc: Array.isArray(timelineData.phase_toc) ? timelineData.phase_toc : [],
        cycles: Array.isArray(timelineData.cycles) ? timelineData.cycles : [],
        issue_affordances: Array.isArray(timelineData.issue_affordances) ? timelineData.issue_affordances : [],
        lifecycle,
        error: timelineData.error || timelineData.detail || '',
    };
}

function _resultCategories(data) {
    if (!data || typeof data !== 'object') return _emptyE2EResultCategories();
    const payload = data.results_by_category && typeof data.results_by_category === 'object'
        ? data.results_by_category
        : {};
    return {
        untriaged: Array.isArray(payload.untriaged) ? payload.untriaged : [],
        has_issue: Array.isArray(payload.has_issue) ? payload.has_issue : [],
        flaky: Array.isArray(payload.flaky) ? payload.flaky : [],
        fixed: Array.isArray(payload.fixed) ? payload.fixed : [],
        passed: Array.isArray(payload.passed) ? payload.passed : [],
        quarantined: Array.isArray(payload.quarantined) ? payload.quarantined : [],
        skipped: Array.isArray(payload.skipped) ? payload.skipped : [],
    };
}

// ``_allResultCases`` and ``_findResultCase`` removed in Phase C
// (PR #6319 Blocker 2) — they were only used by the deleted
// ``copyTestErrorFromRun`` handler.

function _humanizeSnakeCase(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    const normalized = text.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(E2E_LABEL_OVERRIDES, normalized)) {
        return E2E_LABEL_OVERRIDES[normalized];
    }
    return text
        .replace(/_/g, ' ')
        .replace(/\b\w/g, char => char.toUpperCase());
}

function _runStatusClass(status) {
    const normalized = String(status || '').toLowerCase();
    if (normalized === 'passed') return 'passed';
    if (normalized === 'warning') return 'warning';
    if (normalized === 'running') return 'running';
    if (normalized === 'failed' || normalized === 'error') return 'failed';
    return 'muted';
}

function _formatRunnerLabel(run) {
    const runnerKind = String(run && run.runner_kind || '').trim();
    if (!runnerKind) return 'Unknown runner';
    return _humanizeSnakeCase(runnerKind);
}

function _formatRunCommand(run) {
    const command = Array.isArray(run && run.command) ? run.command : [];
    if (command.length > 0) return command.join(' ');
    const pytestArgs = Array.isArray(run && run.pytest_args) ? run.pytest_args : [];
    if (pytestArgs.length > 0) return ['pytest', ...pytestArgs].join(' ');
    return '';
}

function _formatDurationSeconds(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '';
    return `${value.toFixed(1)}s`;
}

function _artifactButton(path, label, cssClass = 'issue-action-btn') {
    if (!path) return '';
    return `<button class="${cssClass}" data-artifact-path="${escapeAttr(path)}" onclick="openE2EArtifactFromButton(this); event.stopPropagation();">${escapeHtml(label)}</button>`;
}

function openE2EArtifactFromButton(button) {
    const path = button && button.dataset ? String(button.dataset.artifactPath || '').trim() : '';
    if (!path) {
        throw new Error('Artifact button missing data-artifact-path');
    }
    if (!window || typeof window.openPath !== 'function') {
        throw new Error('openPath is unavailable');
    }
    window.openPath(path);
}

// Legacy ``_e2eRowActionButton`` / ``runE2ERowActionFromButton``
// removed in Phase C (PR #6319 Blocker 2).  Per-row triage actions
// (Close issue, Create issue, Quarantine, Copy error) are no longer
// the row-action contract: Copy-error is now a built-in icon on the
// canonical viewer's failure card, the linked-issue affordance is
// the ``io.agent-context`` plugin block, and quarantine is deferred
// to issue #6318.

// ``_primaryRunReport`` removed in Phase C — it picked a primary
// report among the run's artifacts but had no remaining caller after
// the per-row triage UI went away.

function _renderRunArtifactButtons(data) {
    const run = data && data.run ? data.run : {};
    const reports = Array.isArray(data && data.reports) ? data.reports : [];
    const artifacts = Array.isArray(data && data.artifacts) ? data.artifacts : [];
    const html = [];

    if (run.log_path) {
        html.push(_artifactButton(run.log_path, 'Raw Output'));
    }
    for (const report of reports) {
        if (!report || !report.path) continue;
        html.push(_artifactButton(report.path, report.label || _humanizeSnakeCase(report.kind)));
    }
    for (const artifact of artifacts) {
        if (!artifact || !artifact.path) continue;
        if (reports.includes(artifact)) continue;
        if (artifact.kind === 'raw_log') continue;
        html.push(_artifactButton(artifact.path, artifact.label || artifact.path, 'issue-action-btn subtle'));
    }
    return html.join('');
}

// Legacy helpers removed in Phase C (PR #6319 Blocker 2):
//   ``_normalizeE2ERunLifecycle`` / ``_lifecycleSessionCommand`` /
//   ``_lifecycleValidationCommand`` / ``_lifecycleReviewTranscriptCommand``
//   — per-row inline lifecycle block; superseded by the
//   ``io.agent-context`` plugin's drawer-open affordance.
//   ``_flattenTestsByCategory`` / ``_lifecyclesByIssueNumber`` —
//   helpers for the old categorized panel.
//   ``_e2eCapturedOutputUrl`` — captured-output URL builder for the
//   legacy panel's lazy-fetch path.
//   ``_renderE2ETestRowActions`` / ``_renderE2EIssueLifecycleBlock`` —
//   per-row triage actions; replaced by the canonical viewer +
//   ``io.agent-context`` plugin.
//
// ``_renderLifecycleCommandButton`` / ``runE2ELifecycleCommandFromButton``
// / ``runE2ELifecycleCommand`` live in the shared
// ``static/js/dashboard/lifecycle_commands.js`` module (loaded before
// this file).

// Phase C (issue #6310 follow-up): the E2E run view's body is now the
// canonical validation viewer.  We translate the E2E run payload to the
// JUnit-canonical shape (via ``e2eRunToCanonicalPayload``), then mount
// the shared viewer.  The filter pills, bulk-action bar, and per-row
// triage actions from the legacy panel are gone — orchestrator-
// specific affordances (Open issue drawer, Create issue, Agent
// journey) live in the per-test ``io.agent-context`` plugin block now;
// the Copy-error button is a built-in canonical-viewer action.
//
// Quarantine UI is deferred to issue #6318 (its design wasn't ready;
// the existing quarantine API + sidebar entrypoints continue to work,
// they're just not surfaced in the redesigned run modal).
function renderE2EResultsPanel(data) {
    const canonical = e2eRunToCanonicalPayload(data);
    const untrackedCount = _untrackedFailureCount(data);
    const viewerHtml = renderCanonicalValidationViewer(canonical);

    return `
        <div class="e2e-canonical-panel">
            ${_renderRunSummaryChips(data, canonical)}
            ${untrackedCount > 0 ? _renderUntrackedFailuresBanner(untrackedCount) : ''}
            <div class="e2e-canonical-body">${viewerHtml}</div>
            ${renderRunDetailsDisclosure(data)}
        </div>
    `;
}

// Run-level summary chips.  Info display only — NOT filter pills.
// Layout: outcome chip + counts + (optional) command/duration meta.
function _renderRunSummaryChips(data, canonical) {
    const status = canonical.status === 'passed' ? 'passed' : 'failed';
    const totalCases = canonical.junit_cases.length;
    const failedCount = canonical.failed_tests.length;
    const passedCount = canonical.junit_cases.filter(c => c.outcome === 'passed').length;
    const skippedCount = canonical.junit_cases.filter(c => c.outcome === 'skipped').length;
    const command = _formatRunCommand(data && data.run);
    const duration = data && data.run ? _formatDurationSeconds(data.run.duration_seconds) : '';

    const chips = [
        `<span class="e2e-run-chip e2e-run-chip-${status}">${status}</span>`,
        `<span class="e2e-run-chip">${totalCases} case${totalCases === 1 ? '' : 's'}</span>`,
    ];
    if (failedCount > 0) chips.push(`<span class="e2e-run-chip is-fail">${failedCount} failing</span>`);
    chips.push(`<span class="e2e-run-chip">${passedCount} passing</span>`);
    if (skippedCount > 0) chips.push(`<span class="e2e-run-chip muted">${skippedCount} skipped</span>`);

    const meta = (command || duration)
        ? `<span class="e2e-run-summary-meta">${command ? escapeHtml(command) : ''}${command && duration ? ' · ' : ''}${duration ? escapeHtml(duration) : ''}</span>`
        : '';
    return `<div class="e2e-run-summary">${chips.join('')}${meta}</div>`;
}

// Untracked-failures banner: only renders when at least one failing
// test has no linked issue.  Hooks the shared agent-picker → Create
// Issues for the orchestrator's existing bulk-create flow.
function _renderUntrackedFailuresBanner(untrackedCount) {
    const agentSelect = (window && window.dashboardData && Array.isArray(window.dashboardData.agents))
        ? window.dashboardData.agents.map(a => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join('')
        : '';
    const plural = untrackedCount === 1 ? 'test has' : 'tests have';
    return `
        <div class="e2e-untracked-banner">
            <span class="e2e-untracked-banner-text">🎯 ${untrackedCount} failing ${plural} no linked issue</span>
            <div class="e2e-untracked-banner-actions">
                <select id="unifiedRunAgent" class="agent-select">
                    <option value="">Select agent…</option>
                    ${agentSelect}
                </select>
                <button class="btn-primary" onclick="createIssuesForUntriaged()">Create issue${untrackedCount === 1 ? '' : 's'}</button>
            </div>
        </div>
    `;
}

function _untrackedFailureCount(data) {
    const untriaged = (data && data.results_by_category && Array.isArray(data.results_by_category.untriaged))
        ? data.results_by_category.untriaged
        : [];
    return untriaged.length;
}

function renderRunDetailsDisclosure(data) {
    const run = data && data.run ? data.run : {};
    const command = _formatRunCommand(run);
    const buttons = _renderRunArtifactButtons(data);
    return `
        <details class="run-details-disclosure" id="runDetailsDisclosure">
            <summary>Run details &amp; artifacts<span class="rdd-summary-hint"> · runner, command, suite artifacts, suite timeline</span></summary>
            <div class="rdd-body">
                <div class="rdd-grid">
                    <div class="rdd-row"><span class="rdd-label">Runner</span><span class="rdd-value">${escapeHtml(_formatRunnerLabel(run))}</span></div>
                    <div class="rdd-row"><span class="rdd-label">Status</span><span class="rdd-value"><span class="e2e-run-status ${_runStatusClass(run.status)}">${escapeHtml(_humanizeSnakeCase(run.status || 'unknown'))}</span></span></div>
                    <div class="rdd-row"><span class="rdd-label">Started</span><span class="rdd-value">${escapeHtml(formatTimestamp(run.started_at) || 'Unknown')}</span></div>
                    <div class="rdd-row"><span class="rdd-label">Duration</span><span class="rdd-value">${escapeHtml(_formatDurationSeconds(run.duration_seconds) || '—')}</span></div>
                    ${run.commit_sha ? `<div class="rdd-row"><span class="rdd-label">Commit</span><span class="rdd-value"><code>${escapeHtml(String(run.commit_sha).substring(0, 12))}</code></span></div>` : ''}
                    ${run.branch ? `<div class="rdd-row"><span class="rdd-label">Branch</span><span class="rdd-value"><code>${escapeHtml(run.branch)}</code></span></div>` : ''}
                    <div class="rdd-row rdd-command-row"><span class="rdd-label">Command</span><span class="rdd-value">${command ? `<code class="e2e-run-command">${escapeHtml(command)}</code>` : 'Unavailable'}</span></div>
                </div>
                ${buttons ? `<div class="rdd-artifacts"><div class="rdd-section-title">Suite artifacts</div><div class="e2e-action-row">${buttons}</div></div>` : ''}
                <div class="rdd-timeline">
                    <div class="rdd-section-title">Suite timeline</div>
                    <div class="e2e-timeline-view-switcher">
                        <button class="e2e-view-btn active" onclick="switchE2ETimelineView('user', this); event.stopPropagation();" data-view="user">Story</button>
                        <button class="e2e-view-btn" onclick="switchE2ETimelineView('ops', this); event.stopPropagation();" data-view="ops">Ops</button>
                        <button class="e2e-view-btn" onclick="switchE2ETimelineView('debug', this); event.stopPropagation();" data-view="debug">Debug</button>
                    </div>
                    <div id="e2eTimelineContent"></div>
                </div>
            </div>
        </details>
    `;
}

function openE2ERunTimeline(runId) {
    // Open run modal and auto-expand the Run details & artifacts
    // disclosure (which holds the suite timeline).  Routed through
    // the typed Command pipeline (issue #6322, PR #6329 reviewer
    // Blocker 2) — single owner for "open E2E run" navigation.
    return runE2ELifecycleCommand({
        kind: 'open_e2e_run',
        label: 'Open E2E Run',
        run_id: Number(runId),
        expand_run_details: true,
    });
}

/**
 * Render the run modal with a test-centric layout: tests are the headline,
 * run metadata + suite artifacts + suite timeline live in a Run details &
 * artifacts disclosure at the bottom.
 */
function renderUnifiedRunView(data, runId, options) {
    options = options || {};
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = document.getElementById('e2eDiagnosisModal').querySelector('.modal-header h2');
    const run = data && data.run ? data.run : {};
    const runDate = run.started_at ? new Date(run.started_at).toLocaleString() : 'Unknown';
    const commitFragment = run.commit_sha ? ` · ${String(run.commit_sha).substring(0, 7)}` : '';
    modalTitle.textContent = `Run #${run.id || runId} · ${runDate}${commitFragment}`;

    const tl = normalizeE2ETimelineData(data);
    const html = `
        <div class="unified-run-view">
            ${run.note ? `<div class="e2e-run-note-banner">${escapeHtml(run.note)}</div>` : ''}
            ${renderE2EResultsPanel(data)}
        </div>
    `;
    content.innerHTML = html;

    // Phase C (issue #6310 follow-up): the canonical viewer is mounted
    // as the body.  Enhance it with the ARIA tree semantics + keyboard
    // nav that the modal and per-issue drawer mounts get.
    const cvvRoot = content.querySelector('.cvv-root');
    if (cvvRoot && typeof enhanceCanonicalValidationViewerAccessibility === 'function') {
        enhanceCanonicalValidationViewerAccessibility(cvvRoot);
    }
    // The legacy ``_autoLoadVisibleCapturedOutput`` lazy-loaded
    // per-row captured stdout/stderr into the old test-results-panel
    // DOM.  The canonical viewer renders its own stdout/stderr rows
    // and doesn't need this entry point.  Captured-output lazy-load
    // for the canonical viewer is a follow-up (no regression in
    // diagnostic info — failure_details still carries the headline +
    // traceback).

    const timelineContainer = document.getElementById('e2eTimelineContent');
    if (timelineContainer) {
        renderE2ETimeline(timelineContainer, tl);
    }

    if (options.expandRunDetails) {
        const disclosure = document.getElementById('runDetailsDisclosure');
        if (disclosure) disclosure.open = true;
    }
}

function renderE2ETimeline(container, timelineData) {
    if (!container) return;
    const tl = normalizeE2ETimelineData(timelineData || {});
    applyLifecycleDataset(container, tl.lifecycle);
    container.innerHTML = `
        ${renderE2EIssueTimelineAffordances(tl.issue_affordances)}
        ${tl.error ? `<div class="timeline-empty e2e-timeline-error">${escapeHtml(tl.error)}</div>` : ''}
        <div class="e2e-timeline-events"></div>
    `;
    const eventsContainer = container.querySelector('.e2e-timeline-events');
    renderTimeline(eventsContainer, tl.events, tl.phase_toc, tl.cycles);
}

function renderE2EIssueTimelineAffordances(affordances) {
    // PR #6319 round 4: the run-level issue-timeline affordance now
    // routes through the shared typed-Command pipeline
    // (``_renderLifecycleCommandButton`` →
    // ``runE2ELifecycleCommandFromButton`` → ``runE2ELifecycleCommand``
    // → ``openIssueTimeline``).  The previous inline ``onclick``
    // path was a second owner for the same UI command — the
    // ``open_issue_timeline`` kind already has a typed shape with
    // ``scope_kind: 'e2e_run'`` / ``e2e_run_id``, so the dispatcher
    // can route it.
    const items = (Array.isArray(affordances) ? affordances : [])
        .map((affordance) => {
            const issueNumber = Number(affordance.issue_number);
            const runId = Number(affordance.run_id);
            if (!Number.isInteger(issueNumber) || !Number.isInteger(runId)) return '';
            const label = affordance.label ? String(affordance.label) : '';
            // The label inside the button matches the prior visual
            // shape: ``#N`` + optional human label, both rendered
            // as a single inline string.  ``_renderLifecycleCommandButton``
            // does its own escapeHtml, so the labelHtml chunk has
            // to be assembled into a plain string label (the typed
            // renderer doesn't take inner-HTML overrides).  To
            // preserve the two-span layout we emit a custom button
            // that still carries ``data-lifecycle-command`` — the
            // shared dispatcher reads only the data attribute.
            const cmd = {
                kind: 'open_issue_timeline',
                issue_number: issueNumber,
                scope_kind: 'e2e_run',
                e2e_run_id: runId,
                label: `Open cycle timeline for issue #${issueNumber}`,
            };
            const labelHtml = label
                ? `<span class="e2e-issue-timeline-label">${escapeHtml(label)}</span>`
                : '';
            const cmdAttr = escapeAttr(JSON.stringify(cmd));
            return `<button class="e2e-issue-timeline-btn"
                data-lifecycle-command="${cmdAttr}"
                onclick="runE2ELifecycleCommandFromButton(this); event.stopPropagation();"
                title="Open cycle timeline for issue #${issueNumber}"
                aria-label="Open cycle timeline for issue #${issueNumber}">
                <span class="e2e-issue-timeline-number">#${issueNumber}</span>${labelHtml}
            </button>`;
        })
        .filter(Boolean);
    const body = items.length
        ? `<div class="e2e-issue-timeline-list">${items.join('')}</div>`
        : '<div class="e2e-empty-note">No linked issue timelines for this run.</div>';
    return `<section class="e2e-issue-timeline-affordances" aria-label="Issue timelines from this E2E run">
        <div class="e2e-issue-timeline-title">Issue timelines</div>
        ${body}
    </section>`;
}

async function switchE2ETimelineView(view, btn) {
    const btns = document.querySelectorAll('.e2e-view-btn');
    btns.forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');

    const runId = unifiedRunData && unifiedRunData.run ? unifiedRunData.run.id : null;
    if (!runId) return;

    const container = document.getElementById('e2eTimelineContent');
    if (!container) return;
    container.innerHTML = '<div class="loading-spinner">Loading...</div>';

    try {
        const data = await _fetchE2ERunDetail(runId, view);
        unifiedRunData = data;
        renderE2ETimeline(container, data);
    } catch (err) {
        container.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(err.message)}</div>`;
    }
}

// Legacy ``_copyTextToClipboard`` + ``copyTestErrorFromRun`` removed
// in Phase C (PR #6319 Blocker 2).  The canonical viewer renders a
// built-in Copy-error icon on every failed triage card and uses
// ``navigator.clipboard.writeText`` directly; that's the single
// owner now.

/**
 * Create issues for all untriaged tests.
 */
async function createIssuesForUntriaged() {
    if (!unifiedRunData) return;

    const agent = document.getElementById('unifiedRunAgent')?.value;
    if (!agent) {
        showToast('Please select an agent', true);
        return;
    }

    const untriaged = _resultCategories(unifiedRunData).untriaged || [];
    if (untriaged.length === 0) {
        showToast('No tests need action', true);
        return;
    }

    const nodeids = untriaged.map(t => t.nodeid);

    try {
        const res = await fetch(`/control/e2e/create-issues/${unifiedRunData.run.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeids, agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issues', true);
            return;
        }

        showToast(`Created parent issue #${data.parent_issue.number} with ${data.sub_issues.length} sub-issue(s)`);

        // Refresh the view
        showUnifiedRunView(unifiedRunData.run.id);

        // Open parent issue in new tab
        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issues: ' + err.message, true);
    }
}

// Legacy per-test action handlers removed in Phase C (PR #6319
// Blocker 2):
//   ``closeE2EIssue``           — Phase C dropped per-test Close-Issue.
//                                 Issues close at orchestrator publish
//                                 time, not from the dashboard.
//   ``showCreateIssueDropdown`` — Phase C dropped per-row Create-Issue
//                                 dropdown.  The bulk
//                                 ``createIssuesForUntriaged`` flow
//                                 (driven by the untracked-failures
//                                 banner) is the single owner now.
//   ``createSingleIssueWithAgent``  — same; superseded by the bulk
//                                 flow + the canonical viewer's
//                                 per-test plugin block (which
//                                 navigates to the per-issue drawer
//                                 for further triage).
