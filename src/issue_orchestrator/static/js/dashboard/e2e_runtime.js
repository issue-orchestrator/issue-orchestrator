const REPO_ROOT = window.dashboardData?.repoRoot
    || new URLSearchParams(window.location.search).get('repo_root')
    || '';
const CONFIG_NAME = window.dashboardData?.configName
    || new URLSearchParams(window.location.search).get('config_name')
    || '';

// Mutable state for E2E - updated by polling
let e2eLastRun = window.dashboardData.e2eLastRun;
let e2eLastStatusData = {
    running: window.dashboardData.e2eRunning,
    last_run: e2eLastRun,
    needs_attention: window.dashboardData.e2eNeedsAttention,
    failed_tests: Array.isArray(window.dashboardData.e2eFailedTests) ? window.dashboardData.e2eFailedTests : [],
};

// E2E Progress Polling - polls while E2E is running or E2E tab is active
let e2ePollingInterval = null;
let e2eLastProgressState = null;

function e2eBadgeStateFromStatus(data) {
    if (data?.running) return 'running';
    const failedTestCount = Array.isArray(data?.failed_tests) ? data.failed_tests.length : 0;
    const status = data?.last_run?.status || '';
    if (data?.needs_attention || failedTestCount > 0 || status === 'failed') return 'failed';
    if (status === 'warning') return 'warning';
    if (status === 'passed') return 'passed';
    return 'idle';
}

function updateE2EHeaderBadge(data) {
    const badge = document.getElementById('e2eHeaderBadge');
    if (!badge) return;

    e2eLastStatusData = { ...e2eLastStatusData, ...(data || {}) };
    const state = e2eBadgeStateFromStatus(e2eLastStatusData);
    const statusIcon = badge.querySelector('.status-icon');
    badge.classList.remove('running', 'passed', 'failed', 'warning', 'idle');
    badge.classList.add(state);

    const statusIcons = { running: '⟳', failed: '✗', warning: '⚠', passed: '✓', idle: '○' };
    if (statusIcon) {
        statusIcon.textContent = statusIcons[state] || '○';
    }
}

function startE2EPolling() {
    if (!e2ePollingInterval) {
        e2ePollingInterval = setInterval(updateE2EProgress, 5000);
        updateE2EProgress();
    }
}

function stopE2EPolling() {
    if (e2ePollingInterval) {
        clearInterval(e2ePollingInterval);
        e2ePollingInterval = null;
    }
}

// Event delegation for triage modal and quarantine actions
document.addEventListener('click', function(e) {
    const target = e.target.closest('[data-action]');
    if (!target || !target.dataset.nodeid) return;

    const action = target.dataset.action;
    const nodeid = target.dataset.nodeid;

    if (action === 'open-test-detail') {
        openTestFailureDetail(nodeid);
    }
});

// Event delegation for quarantine checkbox changes
document.addEventListener('change', function(e) {
    const target = e.target.closest('[data-action]');
    if (!target || !target.dataset.nodeid) return;

    const action = target.dataset.action;
    const nodeid = target.dataset.nodeid;

    if (action === 'quarantine-add') {
        toggleQuarantineAdd(nodeid);
    } else if (action === 'quarantine-remove') {
        toggleQuarantineRemove(nodeid);
    }
});

async function updateE2EProgress() {
    try {
        const res = await fetch(`/control/e2e/status?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        // Update mutable last run state
        if (data.last_run) {
            e2eLastRun = data.last_run;
        }

        // Create state key for comparison
        const stateKey = JSON.stringify({
            running: data.running,
            lastRunStatus: data.last_run?.status,
            lastRunId: data.last_run?.id,
            needsAttention: data.needs_attention,
            failedTestCount: Array.isArray(data.failed_tests) ? data.failed_tests.length : 0,
        });

        // Skip updates if state hasn't changed (reduces visual churn)
        if (stateKey === e2eLastProgressState) {
            return;
        }
        e2eLastProgressState = stateKey;

        updateE2EHeaderBadge(data);

        // Stop polling when not running
        if (!data.running) {
            stopE2EPolling();
        }
    } catch (err) {
        console.error('E2E progress update failed:', err);
    }
}

async function startE2E(forceRestart = false) {
    try {
        // If forcing restart, stop first then start
        if (forceRestart) {
            const stopRes = await fetch('/control/e2e/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_root: REPO_ROOT, config_name: CONFIG_NAME })
            });
            if (!stopRes.ok) {
                showToast('Failed to stop running E2E', 'error');
                return;
            }
            // Brief delay to let worker terminate
            await new Promise(r => setTimeout(r, 500));
        }

        const res = await fetch('/control/e2e/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: REPO_ROOT, config_name: CONFIG_NAME })
        });
        const data = await res.json();
        if (res.ok) {
            showToast('E2E tests started');
            // Update header badge to running state
            updateE2EHeaderBadge({ running: true, last_run: e2eLastRun });

            // Update E2E tab controls if on E2E tab
            const e2eControls = document.getElementById('e2eControls');
            if (e2eControls) {
                e2eControls.innerHTML = `
                    <button class="issue-action-btn stop-btn" onclick="stopE2E()" id="e2eStopBtn">
                        <span aria-hidden="true">⏹</span> Stop E2E
                    </button>
                    <span class="e2e-progress-text" id="e2eProgressText">Running...</span>
                `;
            }

            startE2EPolling();
        } else if (data.error === 'already_running') {
            // Ask user if they want to cancel and restart
            if (confirm('E2E tests are already running.\n\nCancel the current run and start fresh?')) {
                startE2E(true);  // Restart with force flag
            }
        } else {
            showToast(data.detail || data.error || 'Failed to start E2E', 'error');
        }
    } catch (err) {
        showToast('Failed to start E2E: ' + err.message, 'error');
    }
}

async function stopE2E() {
    try {
        const res = await fetch('/control/e2e/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_root: REPO_ROOT, config_name: CONFIG_NAME })
        });
        const data = await res.json();
        if (res.ok) {
            showToast('E2E tests stopped');
            stopE2EPolling();
            // Update header badge to stopped state
            updateE2EHeaderBadge({ running: false, last_run: e2eLastRun });

            // Update E2E tab controls if on E2E tab
            const e2eControls = document.getElementById('e2eControls');
            if (e2eControls) {
                e2eControls.innerHTML = `
                    <button class="issue-action-btn start-btn" onclick="startE2E()" id="e2eStartBtn">
                        <span aria-hidden="true">▶</span> Start E2E Tests
                    </button>
                    <span class="e2e-last-run">Stopped</span>
                `;
            }
        } else {
            showToast(data.detail || 'Failed to stop E2E', true);
        }
    } catch (err) {
        showToast('Failed to stop E2E: ' + err.message, true);
    }
}

// Start polling if E2E is already running on page load
if (window.dashboardData.e2eRunning) {
document.addEventListener('DOMContentLoaded', () => startE2EPolling());
}

async function showE2ELogs() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }
    if (!e2eLastRun.log_path) {
        showToast('No log file for this run', true);
        return;
    }

    try {
        const res = await fetch(`/control/e2e/logs/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&tail=200`);
        const data = await res.json();
        if (res.ok) {
            const content = data.content || 'No logs available';
            // Show in a simple modal/alert for now
            alert(`E2E Logs (last ${data.returned_lines} lines):\n\n${content}`);
        } else {
            showToast(data.detail || 'Failed to fetch logs', true);
        }
    } catch (err) {
        showToast('Failed to fetch E2E logs', true);
    }
}

async function showQuarantineList() {
    try {
        const res = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to load quarantine list', true);
            return;
        }

        let message = `Quarantine List\n`;
        message += `${'='.repeat(40)}\n\n`;
        message += `File: ${data.quarantine_file}\n`;
        message += `Status: ${data.exists ? 'exists' : 'not found'}\n`;
        message += `Count: ${data.count} test(s)\n`;

        if (data.tests.length > 0) {
            message += `\n${'─'.repeat(40)}\nQuarantined Tests:\n\n`;
            for (const test of data.tests) {
                message += `• ${test}\n`;
            }
            message += `\n${'─'.repeat(40)}\n`;
            message += `These tests are excluded from failure counts.\n`;
            message += `Edit ${data.quarantine_file} to modify.`;
        } else {
            message += `\nNo tests are currently quarantined.`;
        }

        alert(message);
    } catch (err) {
        showToast('Failed to load quarantine list: ' + err.message, true);
    }
}

async function showE2EFailures() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    try {
        const res = await fetch(`/control/e2e/summary/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const summary = await res.json();

        if (!res.ok) {
            showToast(summary.error || 'Failed to load test summary', true);
            return;
        }

        const counts = summary.counts;
        let message = `E2E Test Summary (Run #${e2eLastRun.id})\n`;
        message += `${'='.repeat(40)}\n\n`;
        message += `Total: ${counts.total} tests\n`;
        message += `  ✓ Passed: ${counts.passed}\n`;
        message += `  ✗ Failed: ${counts.failed}\n`;

        if (counts.passed_on_retry > 0) {
            message += `  ↻ Passed on Retry: ${counts.passed_on_retry}\n`;
        }
        if (counts.quarantined > 0) {
            message += `  ⚠ Quarantined: ${counts.quarantined}\n`;
        }
        if (counts.skipped > 0) {
            message += `  ○ Skipped: ${counts.skipped}\n`;
        }

        // Show failed tests
        if (summary.failed.length > 0) {
            message += `\n${'─'.repeat(40)}\nFailed Tests:\n`;
            for (const f of summary.failed) {
                message += `\n• ${f.nodeid}\n`;
                if (f.longrepr) {
                    message += `  ${f.longrepr.substring(0, 150)}...\n`;
                }
            }
        }

        // Show passed on retry
        if (summary.passed_on_retry.length > 0) {
            message += `\n${'─'.repeat(40)}\nPassed on Retry (flaky):\n`;
            for (const f of summary.passed_on_retry) {
                message += `• ${f.nodeid}\n`;
            }
        }

        // Show quarantined
        if (summary.quarantined.length > 0) {
            message += `\n${'─'.repeat(40)}\nQuarantined (excluded from failure count):\n`;
            for (const q of summary.quarantined) {
                message += `• ${q.nodeid}\n`;
            }
        }

        alert(message);
    } catch (err) {
        showToast('Failed to load test summary: ' + err.message, true);
    }
}

// E2E Diagnosis state
let e2eCurrentDiagnosis = null;

async function showE2EDiagnosis() {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    // Show modal with loading state
    document.getElementById('e2eDiagnosisContent').innerHTML = '<div class="loading-spinner">Loading diagnosis...</div>';
    document.getElementById('e2eDiagnosisModal').classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/diagnosis/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const diagnosis = await res.json();

        if (!res.ok) {
            showToast(diagnosis.error || diagnosis.detail || 'Failed to load diagnosis', true);
            closeE2EDiagnosisModal();
            return;
        }

        e2eCurrentDiagnosis = diagnosis;
        renderE2EDiagnosis(diagnosis);
    } catch (err) {
        showToast('Failed to load diagnosis: ' + err.message, true);
        closeE2EDiagnosisModal();
    }
}

function renderE2EDiagnosis(diagnosis) {
    const content = document.getElementById('e2eDiagnosisContent');

    let html = `
        <div class="diagnosis-header">
            <span class="diagnosis-status status-${diagnosis.status}">${diagnosis.status}</span>
            <span class="diagnosis-meta">
                Run #${diagnosis.run_id} &middot; ${diagnosis.commit_sha ? diagnosis.commit_sha.slice(0, 7) : 'unknown'} &middot; ${diagnosis.branch || 'unknown'}
                ${diagnosis.duration_seconds ? ` &middot; ${diagnosis.duration_seconds.toFixed(1)}s` : ''}
            </span>
        </div>

        <div class="diagnosis-summary">
            <div class="stat"><span class="label">Total</span><span class="value">${diagnosis.total_tests}</span></div>
            <div class="stat passed"><span class="label">Passed</span><span class="value">${diagnosis.passed_count}</span></div>
            <div class="stat failed"><span class="label">Failed</span><span class="value">${diagnosis.failed_count}</span></div>
            <div class="stat flaky"><span class="label">Flaky</span><span class="value">${diagnosis.passed_on_retry_count}</span></div>
        </div>
    `;

    // Warnings
    if (diagnosis.warnings && diagnosis.warnings.length > 0) {
        html += `
            <div class="diagnosis-section warnings">
                <h3>Warnings</h3>
                <ul>${diagnosis.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul>
            </div>
        `;
    }

    // Suggestions
    if (diagnosis.suggestions && diagnosis.suggestions.length > 0) {
        html += `
            <div class="diagnosis-section suggestions">
                <h3>Suggestions</h3>
                <ul>${diagnosis.suggestions.map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
            </div>
        `;
    }

    // Failed tests
    if (diagnosis.failed_tests && diagnosis.failed_tests.length > 0) {
        html += `
            <div class="diagnosis-section">
                <h3>Failed Tests (${diagnosis.failed_tests.length})</h3>
                ${diagnosis.failed_tests.map(t => `
                    <div class="failed-test">
                        <div class="test-nodeid">${escapeHtml(t.nodeid)}</div>
                        <pre class="test-error">${escapeHtml(t.longrepr || 'No error details')}</pre>
                    </div>
                `).join('')}
            </div>
        `;
    }

    // Flaky tests
    if (diagnosis.flaky_tests && diagnosis.flaky_tests.length > 0) {
        html += `
            <div class="diagnosis-section">
                <h3>Flaky Tests - Passed on Retry (${diagnosis.flaky_tests.length})</h3>
                ${diagnosis.flaky_tests.map(t => `
                    <div class="failed-test">
                        <div class="test-nodeid">${escapeHtml(t.nodeid)}</div>
                        <pre class="test-error">${escapeHtml(t.longrepr || 'No error details')}</pre>
                    </div>
                `).join('')}
            </div>
        `;
    }

    // Log content
    if (diagnosis.log_content) {
        html += `
            <details class="diagnosis-section logs">
                <summary>Full Log Output (${diagnosis.log_content.split('\\n').length} lines)</summary>
                <pre>${escapeHtml(diagnosis.log_content)}</pre>
            </details>
        `;
    } else if (diagnosis.log_path) {
        html += `
            <div class="diagnosis-section">
                <h3>Log File</h3>
                <p style="color: var(--text-muted);">Log file: <code>${escapeHtml(diagnosis.log_path)}</code>
                    <button class="btn-secondary btn-sm" onclick="openPath('${escapeHtml(diagnosis.log_path)}')">Open</button>
                </p>
                <p style="color: var(--warn);">${diagnosis.log_exists ? 'Log content not loaded' : 'Log file not found'}</p>
            </div>
        `;
    }

    content.innerHTML = html;
}

function closeE2EDiagnosisModal() {
    document.getElementById('e2eDiagnosisModal').classList.remove('visible');
}

// E2E Stats Modal
async function showE2EStats() {
    const modal = document.getElementById('e2eStatsModal');
    const content = document.getElementById('e2eStatsContent');
    if (!REPO_ROOT) {
        content.innerHTML = '<div style="color: var(--danger);">Error: no repository selected for E2E stats.</div>';
        modal.classList.add('visible');
        return;
    }

    content.innerHTML = '<div class="loading-spinner">Loading stats...</div>';
    modal.classList.add('visible');

    try {
        const res = await fetch(`/control/e2e/stats?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(data.error || data.detail || 'Failed to load stats')}</div>`;
            return;
        }

        // Render stats
        const passRatePercent = data.pass_rate_percent !== null ? data.pass_rate_percent : '—';
        const passRateClass = data.pass_rate_percent === null ? 'pass-rate-unknown' :
            data.pass_rate_percent >= 90 ? 'pass-rate-good' :
            data.pass_rate_percent >= 50 ? 'pass-rate-warn' : 'pass-rate-bad';
        const passRateFill = data.pass_rate_percent !== null ? Math.min(100, Math.max(0, data.pass_rate_percent)) : 0;

        let html = `
            <div class="stats-section">
                <div class="stats-header">Pass rate (last ${data.runs_analyzed || data.flake_window_runs} runs)</div>
                <div class="stats-pass-rate">
                    <span class="stats-pass-rate-value ${passRateClass}">${passRatePercent}%</span>
                </div>
                <div class="stats-pass-rate-bar">
                    <div class="stats-pass-rate-fill ${passRateClass}" style="width: ${passRateFill}%;"></div>
                </div>
            </div>

            <div class="stats-row">
                <div class="stats-item">
                    <span class="stats-label">Flaky tests:</span>
                    <span class="stats-value">${data.flaky_count}</span>
                    ${data.flaky_count > 0 ? `<button class="btn-link" onclick="showFlakyTestsList()">View List</button>` : ''}
                </div>
            </div>

            <div class="stats-row">
                <div class="stats-item">
                    <span class="stats-label">Quarantined:</span>
                    <span class="stats-value">${data.quarantine_count}</span>
                    <button class="btn-link" onclick="closeE2EStatsModal(); openQuarantineManager();">Manage</button>
                </div>
            </div>
        `;

        if (data.next_check) {
            html += `
                <div class="stats-section stats-next-check">
                    <div class="stats-label">Next check:</div>
                    <div class="stats-value">${escapeHtml(data.next_check)}</div>
                    ${data.next_check_reason ? `<div class="stats-hint" title="Triggers when interval passed and main branch has new commits">(${escapeHtml(data.next_check_reason)})</div>` : ''}
                </div>
            `;
        }

        content.innerHTML = html;
    } catch (err) {
        content.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function closeE2EStatsModal() {
    document.getElementById('e2eStatsModal').classList.remove('visible');
}

async function showFlakyTestsList() {
    closeE2EStatsModal();
    if (!REPO_ROOT) {
        openModal('Flaky Analysis', '<p>No repository selected for E2E flaky analysis.</p>');
        return;
    }

    try {
        const url = `/control/e2e/flaky-tests?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`;
        console.log('[Flaky Analysis] Fetching:', url);
        const res = await fetch(url);
        const text = await res.text();
        let data;
        try {
            data = JSON.parse(text);
        } catch (parseErr) {
            console.error('[Flaky Analysis] Response not JSON:', res.status, text.slice(0, 500));
            openModal('Flaky Analysis', `<p>Unexpected response (HTTP ${res.status}): ${escapeHtml(text.slice(0, 200))}</p>`);
            return;
        }

        if (!res.ok) {
            console.error('[Flaky Analysis] Error response:', res.status, data);
            openModal('Flaky Analysis', `<p>Failed to load flaky tests (HTTP ${res.status}): ${escapeHtml(data.error || data.detail || 'unknown error')}</p>`);
            return;
        }

        if (!data.flaky_tests || data.flaky_tests.length === 0) {
            openModal('Flaky Analysis', '<p>No flaky tests detected in recent runs.</p>');
            return;
        }

        const rows = data.flaky_tests.map(t => {
            const badge = t.is_quarantined ? ' <span class="quarantine-badge">[Q]</span>' : '';
            return `<tr><td>${escapeHtml(t.nodeid)}${badge}</td><td>${t.flip_rate_percent}%</td><td>${t.flip_count} in ${data.window} runs</td></tr>`;
        }).join('');
        openModal('Flaky Analysis', `
            <p>Tests with flip rate &gt; ${data.threshold}%</p>
            <table class="flaky-table"><thead><tr><th>Test</th><th>Flip rate</th><th>Flips</th></tr></thead>
            <tbody>${rows}</tbody></table>
        `);
    } catch (err) {
        console.error('[Flaky Analysis] Fetch failed:', err);
        openModal('Flaky Analysis', `<p>Failed to load flaky tests: ${escapeHtml(err.message)}</p>`);
    }
}

// Current test failure being viewed in the modal
let currentTestFailure = null;

// Open the test failure detail modal
async function openTestFailureDetail(nodeid) {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    const modal = document.getElementById('testFailureModal');
    const content = document.getElementById('testFailureContent');

    // Show modal with loading state
    content.innerHTML = '<div class="loading-spinner">Loading test details...</div>';
    modal.classList.add('visible');

    try {
        // Use the dedicated test detail endpoint
        const res = await fetch(`/control/e2e/test/${e2eLastRun.id}?nodeid=${encodeURIComponent(nodeid)}&repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`);
        const data = await res.json();

        if (!res.ok) {
            content.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(data.error || data.detail || 'Failed to load test details')}</div>`;
            return;
        }

        // Store for action buttons
        currentTestFailure = {
            nodeid,
            test: data.test,
            run: data.run,
            history: data.history,
            history_summary: data.history_summary,
            flake_count: data.flake_count,
            flip_count: data.flip_count,
            flip_rate: data.flip_rate,
            flip_rate_percent: data.flip_rate_percent,
            category: data.category,
            is_likely_flaky: data.is_likely_flaky,
            existing_issue: data.existing_issue,
            log_excerpt: data.log_excerpt,
        };

        // Render the test failure details
        renderTestFailureDetail(currentTestFailure);
    } catch (err) {
        content.innerHTML = `<div style="color: var(--danger);">Failed to load test details: ${escapeHtml(err.message)}</div>`;
    }
}

function renderTestFailureDetail(data) {
    const content = document.getElementById('testFailureContent');
    const test = data.test;
    const run = data.run;
    const shortName = test.nodeid.split('::').pop();

    // Build status line
    let statusParts = [`<strong>FAILED</strong>`];
    if (test.duration_seconds) {
        statusParts.push(`${test.duration_seconds.toFixed(1)}s`);
    }
    if (test.retry_outcome) {
        statusParts.push(`retry: ${test.retry_outcome}`);
    }

    // Build history visualization (last N runs as icons)
    let historyHtml = '';
    if (data.history && data.history.length > 0) {
        const icons = data.history.map(h => {
            if (h.outcome === 'passed') return '<span style="color: var(--ok);">✓</span>';
            if (h.outcome === 'failed' || h.outcome === 'error') return '<span style="color: var(--danger);">✗</span>';
            return '<span style="color: var(--text-muted);">○</span>';
        }).reverse().join(' ');

        const summary = data.history_summary;
        let passRateText = '';
        if (summary.pass_rate !== null) {
            passRateText = ` (${Math.round(summary.pass_rate * 100)}% pass rate)`;
        }

        let flakyWarning = '';
        if (data.category === 'flaky') {
            flakyWarning = `<span style="color: var(--warn); margin-left: 8px;">⚠ Flaky (${data.flip_rate_percent}% flip rate)</span>`;
        } else if (data.category === 'consistently_failing') {
            flakyWarning = `<span style="color: var(--danger); margin-left: 8px;">⚠ Consistently failing</span>`;
        } else if (data.category === 'new_failure') {
            flakyWarning = `<span style="color: var(--accent); margin-left: 8px;">● New failure</span>`;
        } else if (data.category === 'recovered') {
            flakyWarning = `<span style="color: var(--ok); margin-left: 8px;">↑ Recovered</span>`;
        }

        historyHtml = `
            <div class="test-failure-section" style="background: var(--bg); padding: 12px; border-radius: 6px; margin-bottom: 16px;">
                <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 4px;">History (last ${data.history.length} runs):</div>
                <div style="font-size: 16px; letter-spacing: 2px;">${icons}${passRateText}</div>
                ${flakyWarning}
            </div>
        `;
    }

    // Existing issue link
    let existingIssueHtml = '';
    if (data.existing_issue) {
        existingIssueHtml = `
            <div class="test-failure-section" style="background: var(--status-running-bg); padding: 12px; border-radius: 6px; margin-bottom: 16px; border: 1px solid var(--status-running-border);">
                <span style="color: var(--ok);">✓</span>
                <span>Issue already exists: </span>
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${data.existing_issue.github_issue_number}"
                   target="_blank" style="color: var(--accent);">#${data.existing_issue.github_issue_number}</a>
                ${data.existing_issue.resolution ? `<span style="color: var(--text-muted);"> (${data.existing_issue.resolution})</span>` : ''}
            </div>
        `;
    }

    let html = `
        <div class="test-failure-header">
            <span class="status-icon failed">✗</span>
            <div class="test-failure-info">
                <div class="test-failure-nodeid">${escapeHtml(test.nodeid)}</div>
                <div class="test-failure-meta">
                    <span>${statusParts.join(' · ')}</span>
                    ${run.started_at ? `<span><strong>Run:</strong> ${new Date(run.started_at).toLocaleString()}</span>` : ''}
                    ${run.commit_sha ? `<span><strong>Commit:</strong> ${run.commit_sha.substring(0, 7)}</span>` : ''}
                </div>
            </div>
        </div>

        ${historyHtml}
        ${existingIssueHtml}
    `;

    // Error section
    html += `
        <div class="test-failure-section">
            <h3>Error</h3>
            <div class="test-failure-error">${test.longrepr ? escapeHtml(test.longrepr) : '<span style="color: var(--text-muted);">No error details available</span>'}</div>
        </div>
    `;

    // Log excerpt (expandable)
    if (data.log_excerpt) {
        const lineCount = data.log_excerpt.split('\\n').length;
        html += `
            <details class="test-failure-section">
                <summary style="cursor: pointer; color: var(--accent); font-size: 14px; font-weight: 600;">
                    Test Logs (${lineCount} lines)
                </summary>
                <pre class="test-failure-traceback" style="margin-top: 8px;">${escapeHtml(data.log_excerpt)}</pre>
            </details>
        `;
    }

    // "What to do" section with Diagnose button
    html += `
        <div class="test-failure-section" style="margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border);">
            <h3>What To Do</h3>
            <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 12px;">
                Get AI-powered analysis to help understand this failure and suggest fixes.
            </p>
            <button class="btn-primary" onclick="diagnoseCurrentTest()" style="display: flex; align-items: center; gap: 6px;">
                <span>🔍</span> Diagnose This Failure
            </button>
        </div>
    `;

    content.innerHTML = html;
}

async function diagnoseCurrentTest() {
    if (!currentTestFailure) {
        showToast('No test selected', true);
        return;
    }

    // For now, show the full run diagnosis with this test highlighted
    // In the future, this could trigger AI analysis
    showToast('Opening full diagnosis...', false);
    closeTestFailureModal();
    showE2EDiagnosis();
}

function closeTestFailureModal() {
    document.getElementById('testFailureModal').classList.remove('visible');
    currentTestFailure = null;
}

async function createIssueForCurrentTest() {
    if (!currentTestFailure) {
        showToast('No test selected', true);
        return;
    }
    closeTestFailureModal();
    await createSingleIssue(currentTestFailure.nodeid);
}

async function quarantineCurrentTest() {
    if (!currentTestFailure) {
        showToast('No test selected', true);
        return;
    }
    closeTestFailureModal();
    await quarantineSingleTest(currentTestFailure.nodeid);
}

function copyTestError() {
    if (!currentTestFailure || !currentTestFailure.test) {
        showToast('No test selected', true);
        return;
    }

    const test = currentTestFailure.test;
    const text = `Test: ${test.nodeid}\\n\\n` +
        `Outcome: ${test.outcome}\\n` +
        `Duration: ${test.duration_seconds ? test.duration_seconds.toFixed(2) + 's' : 'unknown'}\\n\\n` +
        `Error:\\n${test.longrepr || 'No error details available'}`;

    navigator.clipboard.writeText(text).then(() => {
        showToast('Error details copied to clipboard');
    }).catch(err => {
        showToast('Failed to copy: ' + err.message, true);
    });
}

// Legacy function - redirect to new modal
async function showTestDiagnosis(nodeid) {
    await openTestFailureDetail(nodeid);
}

// Create issue for a single test
async function createSingleIssue(nodeid) {
    if (!e2eLastRun) {
        showToast('No E2E run data available', true);
        return;
    }

    // Get available agents (just the names)
    const agentList = window.dashboardData.agents;
    if (agentList.length === 0) {
        showToast('No agents configured', true);
        return;
    }

    // Use first agent or prompt if multiple
    let agent = agentList[0];
    if (agentList.length > 1) {
        const choice = prompt(`Select agent for this issue:\n\nAvailable: ${agentList.join(', ')}\n\nEnter agent name:`, agentList[0]);
        if (!choice) return;
        if (!agentList.includes(choice)) {
            showToast(`Invalid agent: ${choice}`, true);
            return;
        }
        agent = choice;
    }

    try {
        const res = await fetch(`/control/e2e/create-issues/${e2eLastRun.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                nodeids: [nodeid],
                agent: agent,
            }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issue', true);
            return;
        }

        const testName = nodeid.split('::').pop();
        if (data.parent_issue) {
            showToast(`Created issue #${data.parent_issue.number} for ${testName}`);
            // Open issue in new tab
            if (data.parent_issue.url) {
                window.open(data.parent_issue.url, '_blank');
            }
        } else {
            showToast('Issue created successfully');
        }

        // Refresh to show the new issue
        setTimeout(() => location.reload(), 500);
    } catch (err) {
        showToast('Failed to create issue: ' + err.message, true);
    }
}

// Quarantine a single test
async function quarantineSingleTest(nodeid) {
    if (!confirm(`Add "${nodeid.split('::').pop()}" to quarantine?\n\nQuarantined tests are excluded from E2E failure counts.`)) {
        return;
    }

    try {
        const res = await fetch(`/control/e2e/quarantine?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'add', nodeids: [nodeid] }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to quarantine test', true);
            return;
        }

        showToast(`Added ${nodeid.split('::').pop()} to quarantine`);
        // Refresh to update the UI
        setTimeout(() => location.reload(), 500);
    } catch (err) {
        showToast('Failed to quarantine test: ' + err.message, true);
    }
}

async function createE2EDiagnosticIssue() {
    const runId = e2eCurrentDiagnosis?.run_id
        || currentRunDetails?.run?.id
        || e2eLastRun?.id;
    if (!runId) {
        showToast('No run data available', true);
        return;
    }

    const agentSelect = document.getElementById('e2eDiagnosisAgent');
    const agent = agentSelect.value;
    if (!agent) {
        showToast('Please select an agent to work on this issue', true);
        agentSelect.focus();
        return;
    }

    const btn = document.getElementById('e2eCreateIssueBtn');
    btn.disabled = true;
    btn.textContent = 'Creating...';

    try {
        const res = await fetch(`/control/e2e/diagnosis/${runId}/issue?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent: agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issue', true);
            return;
        }

        showToast(`Issue #${data.issue_number} created!`);
        closeE2EDiagnosisModal();

        if (data.url) {
            window.open(data.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issue: ' + err.message, true);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create Issue';
    }
}

// E2E Triage Functions
