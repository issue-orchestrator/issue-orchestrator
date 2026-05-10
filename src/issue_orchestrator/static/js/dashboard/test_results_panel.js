// Framework-agnostic test-results rendering shared between the E2E run modal
// and (future) issue-cycle validation modal. Loaded as a plain <script> tag
// alongside e2e_run_view.js — both files share the same global namespace, so
// cross-file function references resolve at call time.

// ── Test-centric layout (framework-agnostic per-test rendering) ────────────
//
// The functions below render a flat, filterable list of tests where each row
// is the primary unit and per-row expansion shows error detail and (when the
// test is linked to a tracked issue) the agentic cycle history inline.
//
// Run-level metadata, suite artifacts, and the suite timeline live in a
// collapsed "Run evidence" disclosure at the bottom — they're diagnostic
// surfaces, not the headline.

const RESULT_CATEGORY_OUTCOME_STATE = new Map([
    ['failed', 'failed'],
    ['fixed', 'passed'],
    ['has_issue', 'failed'],
    ['passed', 'passed'],
    ['quarantined', 'quarantined'],
    ['skipped', 'skipped'],
    ['untriaged', 'failed'],
]);

const ACTION_NEEDED_RESULT_CATEGORIES = new Set(['fixed', 'untriaged']);

function _testFilterGroup(test) {
    if (!test) return 'other';
    const category = _testResultCategory(test);
    const outcomeState = _testOutcomeState(test);
    // Actionable categories intentionally win over raw outcome filters:
    // a fixed/passed test still needs the linked issue closed.
    if (_testNeedsAction(test)) return 'action_needed';
    if (_testIsTrackedFailure(test)) return 'tracked';
    if (outcomeState === 'passed_on_retry') return 'passed_on_retry';
    if (outcomeState === 'passed') return 'passed';
    if (outcomeState === 'failed') return 'failed';
    if (outcomeState === 'skipped') return 'skipped';
    if (outcomeState === 'quarantined') return 'quarantined';
    return 'other';
}

function _testResultCategory(test) {
    const resultCategory = String(test && test.result_category || '').toLowerCase();
    if (resultCategory) return resultCategory;
    return String(test && test.category || '').toLowerCase();
}

function _testEffectiveOutcome(test) {
    return String(test && (test.retry_outcome || test.outcome) || '').toLowerCase();
}

function _testErrorText(test) {
    if (!test || typeof test !== 'object') return '';
    const longrepr = String(test.longrepr || '').trim();
    if (longrepr) return longrepr;
    return String(test.failure_summary || '').trim();
}

function _testOutcomeState(test) {
    if (!test) return 'unknown';
    const category = _testResultCategory(test);
    const outcome = String(test.outcome || '').toLowerCase();
    const effectiveOutcome = _testEffectiveOutcome(test);
    const retryOutcome = String(test.retry_outcome || '').toLowerCase();
    if (test.is_quarantined || category === 'quarantined') return 'quarantined';
    if (outcome === 'failed' && retryOutcome === 'passed') return 'passed_on_retry';
    const categoryOutcomeState = RESULT_CATEGORY_OUTCOME_STATE.get(category);
    if (categoryOutcomeState) return categoryOutcomeState;
    if (effectiveOutcome === 'passed') return 'passed';
    if (effectiveOutcome === 'skipped') return 'skipped';
    if (effectiveOutcome === 'failed' || effectiveOutcome === 'error') return 'failed';
    if (category) return 'failed';
    return 'unknown';
}

function _testNeedsAction(test) {
    const category = _testResultCategory(test);
    const outcomeState = _testOutcomeState(test);
    if (ACTION_NEEDED_RESULT_CATEGORIES.has(category)) return true;
    if (category === 'flaky' && outcomeState === 'failed') return true;
    return false;
}

function _testIsTrackedFailure(test) {
    return _testResultCategory(test) === 'has_issue' && _testOutcomeState(test) === 'failed';
}

function _testOutcomeCounts(tests) {
    const cases = Array.isArray(tests) ? tests : [];
    return {
        total: cases.length,
        failed: cases.filter(t => _testOutcomeState(t) === 'failed').length,
        passed: cases.filter(t => _testOutcomeState(t) === 'passed').length,
        passed_on_retry: cases.filter(t => _testOutcomeState(t) === 'passed_on_retry').length,
        skipped: cases.filter(t => _testOutcomeState(t) === 'skipped').length,
        quarantined: cases.filter(t => _testOutcomeState(t) === 'quarantined').length,
        action_needed: cases.filter(_testNeedsAction).length,
    };
}

function renderTestResultsHeadline(tests) {
    const counts = _testOutcomeCounts(tests);
    return `
        <div class="test-results-headline" role="status" aria-label="Test summary"
             data-total-count="${counts.total}"
             data-failed-count="${counts.failed}"
             data-passed-count="${counts.passed}"
             data-passed-on-retry-count="${counts.passed_on_retry}"
             data-skipped-count="${counts.skipped}"
             data-quarantined-count="${counts.quarantined}"
             data-action-needed-count="${counts.action_needed}">
            <span class="trh-stat trh-total">${counts.total} tests</span>
            <span class="trh-stat trh-failed ${counts.failed ? '' : 'trh-zero'}">✗ ${counts.failed} failed</span>
            <span class="trh-stat trh-passed ${counts.passed ? '' : 'trh-zero'}">✓ ${counts.passed} passed</span>
            ${counts.passed_on_retry ? `<span class="trh-stat trh-warning">⚠ ${counts.passed_on_retry} passed on retry</span>` : ''}
            ${counts.action_needed ? `<span class="trh-stat trh-action">⚠ ${counts.action_needed} action needed</span>` : ''}
            ${counts.skipped ? `<span class="trh-stat trh-skipped">○ ${counts.skipped} skipped</span>` : ''}
            ${counts.quarantined ? `<span class="trh-stat trh-quarantined">⊘ ${counts.quarantined} quarantined</span>` : ''}
        </div>
    `;
}

function renderTestResultsFilters(counts, activeKey = 'all') {
    const chips = [
        { key: 'all', label: `All (${counts.all})` },
        { key: 'action_needed', label: `Action needed (${counts.action_needed})`, hide: counts.action_needed === undefined || !counts.action_needed },
        { key: 'tracked', label: `Tracked failures (${counts.tracked})`, hide: counts.tracked === undefined || !counts.tracked },
        { key: 'failed', label: `Failed (${counts.failed})`, hide: counts.failed === undefined || !counts.failed },
        { key: 'passed_on_retry', label: `Passed on retry (${counts.passed_on_retry})`, hide: counts.passed_on_retry === undefined || !counts.passed_on_retry },
        { key: 'passed', label: `Passed (${counts.passed})`, hide: !counts.passed },
        { key: 'skipped', label: `Skipped (${counts.skipped})`, hide: !counts.skipped },
        { key: 'quarantined', label: `Quarantined (${counts.quarantined})`, hide: !counts.quarantined },
    ];
    return `
        <div class="test-results-filters" role="tablist" aria-label="Filter tests by outcome">
            ${chips
                .filter(c => !c.hide)
                .map(c => `<button type="button" class="trf-chip ${c.key === activeKey ? 'active' : ''}" data-filter="${c.key}" role="tab" aria-selected="${c.key === activeKey}" onclick="filterTestResults('${c.key}', this); event.stopPropagation();">${escapeHtml(c.label)}</button>`)
                .join('')}
        </div>
    `;
}

function _renderTestFailureSummary(test) {
    const outcomeState = _testOutcomeState(test);
    if (outcomeState !== 'failed') return '';
    const summary = String(test.failure_summary || '').trim()
        || _testErrorText(test).split('\n').find(line => line.trim()) || '';
    if (!summary) {
        return '<div class="test-failure-summary muted">No failure text was recorded for this test.</div>';
    }
    return `<div class="test-failure-summary">${escapeHtml(summary)}</div>`;
}

function _renderTestResultPills(test) {
    const outcomeState = _testOutcomeState(test);
    const category = _testResultCategory(test);
    const pills = [];
    const primaryLabels = {
        failed: 'Failed',
        passed: 'Passed',
        passed_on_retry: 'Passed on retry',
        skipped: 'Skipped',
        quarantined: 'Quarantined',
        unknown: 'Unknown',
    };
    pills.push(`<span class="test-result-pill primary ${outcomeState}">${primaryLabels[outcomeState] || 'Unknown'}</span>`);
    if (_testNeedsAction(test)) {
        pills.push('<span class="test-result-pill action-needed">Action needed</span>');
    } else if (_testIsTrackedFailure(test)) {
        pills.push('<span class="test-result-pill tracked">Tracked</span>');
    }
    // Flakiness used to be a peer pill alongside Passed/Failed, which read as a
    // contradictory second outcome. It's now a small annotation on the row's
    // history cluster — see _renderTestRow. Keep this branch as a fallback for
    // tests where the flake flag is set but no per-run history is available.
    if ((test.is_likely_flaky || category === 'flaky') && !_hasHistory(test)) {
        pills.push('<span class="test-result-flaky-note" title="Marked as historically flaky">flaky history</span>');
    }
    if (category === 'fixed') {
        pills.push('<span class="test-result-pill action-needed">Issue can close</span>');
    }
    return `<div class="test-result-pills">${pills.join('')}</div>`;
}

function _hasHistory(test) {
    return Array.isArray(test && test.history) && test.history.length > 0;
}

function _renderTestRowExpand(test, lifecycle, opts) {
    opts = opts || {};
    const errorText = _testErrorText(test);
    const outcomeState = _testOutcomeState(test);
    const errorBlock = outcomeState === 'failed'
        ? `<div class="trr-error">
            <div class="trr-expand-heading">Failure details</div>
            ${errorText
                ? `<pre class="trr-error-text">${escapeHtml(errorText)}</pre>`
                : '<div class="e2e-empty-note">No full error text was recorded for this test.</div>'}
        </div>`
        : '';
    // Captured stdout/stderr is read on-demand from the run's JUnit XML — we
    // never persist it. Render a placeholder; toggleTestRowExpand fills it in
    // on first expand. Any JUnit-sourced row qualifies, regardless of outcome:
    // a green run can still have meaningful captured output (debug prints,
    // setup logs), and not exposing it leaves the user with no UI path at all.
    // Tests with no captured output get a graceful "No captured output
    // recorded for this test." note via the 404 path.
    const runId = Number.isFinite(opts.runId) ? Number(opts.runId) : null;
    const sourceKey = String(test && test.result_source || '').toLowerCase();
    const canFetchOutput = runId !== null && sourceKey.includes('junit');
    const capturedBlock = canFetchOutput
        ? `<div class="trr-captured-output" data-needs-fetch="1" data-run-id="${runId}" data-nodeid="${escapeAttr(test.nodeid)}">
            <div class="trr-expand-heading">Captured output</div>
            <div class="trr-captured-status">Loading captured output…</div>
        </div>`
        : '';
    let lifecycleBlock = '';
    if (test.existing_issue && lifecycle) {
        const issueNumber = Number(lifecycle.issue_number);
        const cycles = Array.isArray(lifecycle.cycles) ? lifecycle.cycles : [];
        const latestCycle = cycles.length ? cycles[cycles.length - 1] : null;
        const timelineCommand = {
            kind: 'open_issue_timeline',
            issue_number: issueNumber,
            scope_kind: 'e2e_run',
            e2e_run_id: Number(unifiedRunData && unifiedRunData.run ? unifiedRunData.run.id : 0) || 0,
            label: `Issue #${issueNumber}`,
        };
        const coderCmd = latestCycle ? _lifecycleSessionCommand(latestCycle.coder && latestCycle.coder.session_recording) : null;
        const reviewCmd = latestCycle ? _lifecycleSessionCommand(latestCycle.review && latestCycle.review.session_recording) : null;
        const transcriptCmd = latestCycle ? _lifecycleReviewTranscriptCommand(issueNumber, latestCycle) : null;
        const validationCmd = latestCycle ? _lifecycleValidationCommand(issueNumber, latestCycle) : null;
        const cycleChips = cycles.map(c => `<span class="e2e-lifecycle-chip">Cycle ${escapeHtml(c.cycle_number)} · ${escapeHtml(_humanizeSnakeCase(c.outcome || 'unknown'))}</span>`).join('');
        lifecycleBlock = `
            <div class="trr-lifecycle">
                <div class="trr-lifecycle-heading">Related issue activity · Issue #${issueNumber}${lifecycle.title ? ` — ${escapeHtml(lifecycle.title)}` : ''}</div>
                <div class="trr-lifecycle-cycles">${cycleChips || '<span class="e2e-empty-note">No cycles projected.</span>'}</div>
                <div class="trr-lifecycle-actions">
                    ${_renderLifecycleCommandButton(timelineCommand, 'Timeline', 'action-btn primary')}
                    ${_renderLifecycleCommandButton(coderCmd, 'Coder Session', 'action-btn subtle')}
                    ${_renderLifecycleCommandButton(reviewCmd, 'Review Session', 'action-btn subtle')}
                    ${_renderLifecycleCommandButton(transcriptCmd, 'Review Transcript', 'action-btn subtle')}
                    ${_renderLifecycleCommandButton(validationCmd, 'Validation', 'action-btn subtle')}
                </div>
            </div>
        `;
    }
    if (!errorBlock && !lifecycleBlock && !capturedBlock) {
        return '';
    }
    // Failed rows render expanded by default — failures are the headline of a
    // test-centric view and shouldn't hide behind a caret. Anything else stays
    // collapsed so the list scans cleanly.
    const startOpen = outcomeState === 'failed';
    const hiddenAttr = startOpen ? '' : 'hidden';
    return `<div class="trr-expand" ${hiddenAttr}>${errorBlock}${capturedBlock}${lifecycleBlock}</div>`;
}

function _renderTestRowActions(test) {
    const category = _testResultCategory(test);
    const outcomeState = _testOutcomeState(test);
    const hasErrorText = Boolean(_testErrorText(test));
    const copyErrorButton = _e2eRowActionButton(hasErrorText ? 'Copy Error' : 'No Error Text', {
        action: 'copy_test_error',
        cssClass: 'action-btn',
        nodeid: test.nodeid,
        disabled: !hasErrorText,
        title: hasErrorText ? 'Copy the failure text for this test' : 'No failure text was recorded for this test',
    });
    if (test.existing_issue) {
        const issueNum = test.existing_issue.number;
        const issueStatus = test.existing_issue.status;
        const ghLink = `<a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}" target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">#${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span></a>`;
        if (category === 'fixed' && issueStatus === 'open') {
            return `${ghLink}${_e2eRowActionButton(`Close #${issueNum}`, { action: 'close_issue', cssClass: 'action-btn success', issueNumber: issueNum, nodeid: test.nodeid })}`;
        }
        return outcomeState === 'failed' ? `${ghLink}${copyErrorButton}` : ghLink;
    }
    if (_testNeedsAction(test) && outcomeState === 'failed') {
        return [
            _e2eRowActionButton('Create Issue ▼', { action: 'create_issue_dropdown', cssClass: 'action-btn primary', nodeid: test.nodeid }),
            _e2eRowActionButton('Quarantine', { action: 'quarantine_test', cssClass: 'action-btn warning', nodeid: test.nodeid }),
            copyErrorButton,
        ].join('');
    }
    if (outcomeState === 'failed') return copyErrorButton;
    return '';
}

function _renderTestRow(test, lifecycle, activeFilter, opts) {
    activeFilter = activeFilter || 'all';
    opts = opts || {};
    const shortName = test.label || test.display_name || (test.nodeid || '').split('::').pop() || test.nodeid;
    const outcomeState = _testOutcomeState(test);
    const outcomeIcon = outcomeState === 'passed' || outcomeState === 'passed_on_retry'
        ? '✓'
        : outcomeState === 'skipped'
            ? '○'
            : outcomeState === 'quarantined'
                ? '⊘'
                : '✗';
    const outcomeClass = outcomeState === 'passed' || outcomeState === 'passed_on_retry'
        ? 'passed'
        : outcomeState === 'skipped' || outcomeState === 'quarantined'
            ? 'skipped'
            : 'failed';
    const filterGroup = _testFilterGroup(test);
    const expand = _renderTestRowExpand(test, lifecycle, opts);
    const expandable = Boolean(expand);
    // The expansion renders open by default for failed rows (see
    // _renderTestRowExpand) — match the caret + aria-expanded state to that
    // initial visibility so screen readers and keyboard users see the truth.
    const startOpen = expandable && outcomeState === 'failed';
    const caretChar = startOpen ? '▾' : '▸';
    const ariaExpandedAttr = startOpen ? 'true' : 'false';
    const historyCluster = _renderHistoryCluster(test);
    const duration = test.duration_seconds ? `<span class="duration">${test.duration_seconds.toFixed(1)}s</span>` : '';
    // Suppress the JUnit provenance tag by default — JUnit XML is the
    // framework-agnostic lingua franca, not a notable per-row signal. Same for
    // the legacy "runtime" source. Only render the tag when the source is
    // something a user would actually want to flag (e.g. external_report).
    const sourceKey = String(test.result_source || '').toLowerCase();
    const sourceTag = sourceKey && sourceKey !== 'runtime' && sourceKey !== 'junit_xml'
        ? `<span class="test-source">${escapeHtml(_humanizeSnakeCase(test.result_source))}</span>`
        : '';
    const suiteHtml = test.suite_name ? `<div class="test-suite" title="${escapeHtml(test.suite_name)}">${escapeHtml(test.suite_name)}</div>` : '';
    const actions = _renderTestRowActions(test);
    const hiddenForDefaultFilter = activeFilter !== 'all' && filterGroup !== activeFilter;
    return `
        <div class="trr-row test-row ${outcomeState}" data-nodeid="${escapeAttr(test.nodeid)}" data-filter-group="${filterGroup}" data-expandable="${expandable ? '1' : '0'}" ${hiddenForDefaultFilter ? 'style="display: none;"' : ''}>
            <div class="trr-row-main test-row-main" ${expandable ? `onclick="toggleTestRowExpand(this); event.stopPropagation();"` : ''} ${expandable ? `role="button" tabindex="0" aria-expanded="${ariaExpandedAttr}"` : ''}>
                ${expandable ? `<span class="trr-caret" aria-hidden="true">${caretChar}</span>` : '<span class="trr-caret-spacer" aria-hidden="true"></span>'}
                <span class="status-icon ${outcomeClass}">${outcomeIcon}</span>
                <div class="trr-row-copy test-row-copy">
                    ${_renderTestResultPills(test)}
                    <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                    ${suiteHtml}
                    ${_renderTestFailureSummary(test)}
                </div>
                ${sourceTag}
                ${historyCluster}
                ${duration}
                ${actions ? `<div class="test-actions trr-row-actions">${actions}</div>` : ''}
            </div>
            ${expand}
        </div>
    `;
}

function _renderHistoryCluster(test) {
    if (!_hasHistory(test)) return '';
    const glyphs = test.history
        .map(h => h.outcome === 'passed'
            ? '<span class="hist-icon pass">✓</span>'
            : h.outcome === 'failed'
                ? '<span class="hist-icon fail">✗</span>'
                : '<span class="hist-icon skip">○</span>')
        .reverse()
        .join('');
    const flakePct = Number(test.flip_rate_percent || 0);
    const flakeAnnotation = flakePct > 0
        ? `<span class="test-history-flake" title="Flip rate across recent runs">· ${flakePct}% flake</span>`
        : '';
    return `<span class="test-history" title="Outcome of this test in the most recent runs (newest first).">
        <span class="test-history-label">Recent</span>
        <span class="test-history-glyphs">${glyphs}</span>
        ${flakeAnnotation}
    </span>`;
}

function toggleTestRowExpand(headerEl) {
    const row = headerEl && headerEl.closest ? headerEl.closest('.trr-row') : null;
    if (!row || row.dataset.expandable !== '1') return;
    const expand = row.querySelector('.trr-expand');
    if (!expand) return;
    const isExpanded = !expand.hasAttribute('hidden');
    if (isExpanded) {
        expand.setAttribute('hidden', '');
        headerEl.setAttribute('aria-expanded', 'false');
        const caret = headerEl.querySelector('.trr-caret');
        if (caret) caret.textContent = '▸';
    } else {
        expand.removeAttribute('hidden');
        headerEl.setAttribute('aria-expanded', 'true');
        const caret = headerEl.querySelector('.trr-caret');
        if (caret) caret.textContent = '▾';
        _maybeLoadCapturedOutput(expand);
    }
}

function _autoLoadVisibleCapturedOutput(root) {
    // A row is "visible" for auto-fetch purposes when neither the row nor an
    // ancestor has display:none (which is how the initial filter hides
    // non-matching rows). This guards a failure-heavy run against firing N
    // parallel fetches for tracked rows that the active filter has hidden.
    root.querySelectorAll('.trr-expand:not([hidden])').forEach(expand => {
        const row = expand.closest && expand.closest('.trr-row');
        if (row && row.style && row.style.display === 'none') return;
        _maybeLoadCapturedOutput(expand);
    });
}

function _maybeLoadCapturedOutput(expand) {
    const placeholder = expand.querySelector('.trr-captured-output[data-needs-fetch="1"]');
    if (!placeholder) return;
    placeholder.dataset.needsFetch = '0';
    const runId = Number(placeholder.dataset.runId);
    const nodeid = placeholder.dataset.nodeid;
    if (!Number.isFinite(runId) || !nodeid) {
        _renderCapturedOutputError(placeholder, 'Cannot load captured output: missing run or test id.');
        return;
    }
    const url = `/api/e2e-run/${runId}/test-output?nodeid=${encodeURIComponent(nodeid)}`;
    fetch(url)
        .then(async response => {
            if (response.status === 404) {
                _renderCapturedOutputEmpty(placeholder);
                return;
            }
            if (!response.ok) {
                _renderCapturedOutputError(placeholder, `Server returned ${response.status}.`);
                return;
            }
            const body = await response.json().catch(() => null);
            if (!body || typeof body !== 'object') {
                _renderCapturedOutputError(placeholder, 'Server returned a malformed response.');
                return;
            }
            _renderCapturedOutputBody(placeholder, body);
        })
        .catch(err => {
            _renderCapturedOutputError(placeholder, escapeHtml(String(err && err.message || err)));
        });
}

function _renderCapturedOutputEmpty(placeholder) {
    placeholder.querySelector('.trr-captured-status').outerHTML =
        '<div class="e2e-empty-note">No captured output recorded for this test.</div>';
}

function _renderCapturedOutputError(placeholder, message) {
    placeholder.querySelector('.trr-captured-status').outerHTML =
        `<div class="e2e-empty-note trr-captured-error">Failed to load captured output: ${message}</div>`;
}

function _renderCapturedOutputBody(placeholder, body) {
    const stdoutHtml = body.system_out
        ? `<div class="trr-captured-channel">
            <div class="trr-captured-channel-label">stdout</div>
            <pre class="trr-error-text trr-captured-text">${escapeHtml(body.system_out)}</pre>
        </div>`
        : '';
    const stderrHtml = body.system_err
        ? `<div class="trr-captured-channel">
            <div class="trr-captured-channel-label">stderr</div>
            <pre class="trr-error-text trr-captured-text">${escapeHtml(body.system_err)}</pre>
        </div>`
        : '';
    placeholder.querySelector('.trr-captured-status').outerHTML = stdoutHtml + stderrHtml;
}

function filterTestResults(filterKey, btnEl) {
    // Scope the filter to the panel containing the clicked chip. Both the
    // E2E run modal and the issue-detail drawer can render a
    // .test-results-panel concurrently — looking up the list/chips
    // globally would target the wrong panel when both are open.
    const panel = btnEl && btnEl.closest ? btnEl.closest('.test-results-panel') : null;
    if (!panel) return;
    const list = panel.querySelector('.test-results-list');
    if (!list) return;
    list.querySelectorAll('.trr-row').forEach(row => {
        const group = row.dataset.filterGroup || 'other';
        const show = filterKey === 'all' || group === filterKey;
        row.style.display = show ? '' : 'none';
    });
    panel.querySelectorAll('.trf-chip').forEach(chip => {
        const active = chip === btnEl;
        chip.classList.toggle('active', active);
        chip.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    // A row that was filter-hidden at initial render skipped its auto-fetch.
    // Now that the user revealed it, kick off the fetch (idempotent — already-
    // fetched rows are no-ops via the placeholder's data-needs-fetch flag).
    _autoLoadVisibleCapturedOutput(panel);
}
