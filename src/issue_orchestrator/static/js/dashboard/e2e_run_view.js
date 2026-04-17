let unifiedRunData = null;  // Stores data for the current unified run view

/**
 * Show the unified run view for any E2E run.
 * This is the main entry point - called when clicking any run row.
 *
 * @param {number} runId - The E2E run ID to display
 */
async function showUnifiedRunView(runId) {
    // Use the diagnosis modal as the container
    const modal = document.getElementById('e2eDiagnosisModal');
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = modal.querySelector('.modal-header h2');

    // Show modal with loading state
    modalTitle.textContent = `E2E Run #${runId}`;
    content.innerHTML = '<div class="loading-spinner">Loading run details...</div>';
    modal.classList.add('visible');

    try {
        // Fetch run details and timeline in parallel
        const [detailsRes, timelineRes] = await Promise.all([
            fetch(`/control/e2e/run/${runId}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&enhanced=true`),
            fetch(`/control/e2e/run/${runId}/timeline?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`),
        ]);
        const data = await detailsRes.json();

        let timelineData = null;
        if (timelineRes.ok) {
            const tl = await timelineRes.json();
            timelineData = {
                events: tl.events || [],
                phase_toc: tl.phase_toc || [],
                cycles: tl.cycles || [],
            };
        }

        if (!detailsRes.ok) {
            content.innerHTML = `<div style="color: var(--danger); padding: 20px;">Error: ${escapeHtml(data.error || data.detail || 'Failed to load run details')}</div>`;
            return;
        }

        unifiedRunData = data;
        unifiedRunData._timeline = timelineData;
        renderUnifiedRunView(data, runId);
    } catch (err) {
        content.innerHTML = `<div style="color: var(--danger); padding: 20px;">Failed to load run details: ${escapeHtml(err.message)}</div>`;
    }
}

/**
 * Render the unified run view with tests grouped by category.
 */
function renderUnifiedRunView(data, runId) {
    const content = document.getElementById('e2eDiagnosisContent');
    const modalTitle = document.getElementById('e2eDiagnosisModal').querySelector('.modal-header h2');
    const run = data.run;
    const summary = data.summary;
    const tests = data.tests_by_category;

    // Update modal title with run info
    const runDate = run.started_at ? new Date(run.started_at).toLocaleString() : 'Unknown';
    modalTitle.textContent = `Run #${run.id} - ${runDate}`;

    const tl = data._timeline || {};
    const hasTimeline = tl.events && tl.events.length > 0;

    // Build header with run info, summary, and tab switcher
    let html = `
        <div class="unified-run-view">
        <div class="unified-run-header">
            <div class="run-meta">
                ${run.commit_sha ? `<span class="commit">Commit: <code>${run.commit_sha.substring(0, 7)}</code></span>` : ''}
                <span class="stat">${summary.total} tests</span>
                ${summary.passed > 0 ? `<span class="stat passed">${summary.passed} passed</span>` : ''}
                ${summary.untriaged + summary.has_issue > 0 ? `<span class="stat failed">${summary.untriaged + summary.has_issue} failed</span>` : ''}
            </div>
            ${hasTimeline ? `
            <div class="e2e-run-tabs">
                <button class="e2e-run-tab active" onclick="switchE2ERunTab('tests', this)" data-tab="tests">Tests</button>
                <button class="e2e-run-tab" onclick="switchE2ERunTab('timeline', this)" data-tab="timeline">Timeline</button>
            </div>` : ''}
        </div>
    `;

    // Tests tab panel
    html += '<div id="e2eRunTestsTab" class="e2e-run-tab-panel">';

    // Render each category section
    html += renderCategorySection('untriaged', 'UNTRIAGED', tests.untriaged,
        'Consistently failing tests with no GitHub issue',
        'warning');

    html += renderCategorySection('has_issue', 'HAS ISSUE', tests.has_issue,
        'Failing tests already tracked by a GitHub issue',
        'info');

    html += renderCategorySection('flaky', 'FLAKY', tests.flaky,
        'Unstable tests (flip rate > threshold) - passed OR failed this run',
        'flaky');

    html += renderCategorySection('fixed', 'FIXED', tests.fixed,
        'Passed this run but has an open issue that should be closed',
        'success');

    html += renderCategorySection('passed', 'PASSED', tests.passed,
        'Stable passing tests',
        'passed', true);  // collapsed by default

    if (tests.quarantined && tests.quarantined.length > 0) {
        html += renderCategorySection('quarantined', 'QUARANTINED', tests.quarantined,
            'Tests excluded from E2E failure counts',
            'quarantined', true);
    }

    if (tests.skipped && tests.skipped.length > 0) {
        html += renderCategorySection('skipped', 'SKIPPED', tests.skipped,
            'Tests that were skipped during this run',
            'skipped', true);
    }

    // Add bulk action bar for untriaged tests
    if (tests.untriaged && tests.untriaged.length > 0) {
        html += `
            <div class="bulk-action-bar">
                <span class="bulk-info">${tests.untriaged.length} untriaged test(s)</span>
                <div class="bulk-actions">
                    <select id="unifiedRunAgent" class="agent-select">
                        <option value="">Select agent...</option>
                        ${window.dashboardData.agents.map(a => `<option value="${a}">${a}</option>`).join('')}
                    </select>
                    <button class="btn-primary" onclick="createIssuesForUntriaged()">
                        Create Issues
                    </button>
                </div>
            </div>
        `;
    }

    // Close tests tab panel
    html += '</div>';

    // Timeline tab panel (hidden by default, populated on tab switch)
    if (hasTimeline) {
        html += `<div id="e2eRunTimelineTab" class="e2e-run-tab-panel" style="display: none;">
            <div class="e2e-timeline-view-switcher">
                <button class="e2e-view-btn active" onclick="switchE2ETimelineView('user', this)" data-view="user">Story</button>
                <button class="e2e-view-btn" onclick="switchE2ETimelineView('ops', this)" data-view="ops">Ops</button>
                <button class="e2e-view-btn" onclick="switchE2ETimelineView('debug', this)" data-view="debug">Debug</button>
            </div>
            <div id="e2eTimelineContent"></div>
        </div>`;
    }

    // Close the unified-run-view wrapper
    html += '</div>';

    content.innerHTML = html;

    // Pre-render timeline if available
    if (hasTimeline) {
        const timelineContainer = document.getElementById('e2eTimelineContent');
        renderTimeline(timelineContainer, tl.events, tl.phase_toc || [], tl.cycles || []);
    }
}

/**
 * Render a category section with its tests.
 */
function renderCategorySection(categoryKey, title, tests, description, styleClass, collapsed = false) {
    if (!tests || tests.length === 0) return '';

    const isCollapsible = collapsed || tests.length > 5;
    const expanded = !collapsed;

    let html = `
        <div class="category-section ${categoryKey}" data-category="${categoryKey}">
            <div class="category-header" ${isCollapsible ? `onclick="toggleCategorySection('${categoryKey}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleCategorySection('${categoryKey}');}" role="button" tabindex="0" aria-expanded="${expanded}"` : ''}>
                <span class="title">${title}</span>
                <span class="count">${tests.length}</span>
                ${isCollapsible ? `<span class="toggle-icon" id="toggle-${categoryKey}"></span>` : ''}
            </div>
            <div class="category-description">${description}</div>
            <div class="category-tests" id="tests-${categoryKey}" style="${collapsed ? 'display: none;' : ''}">
    `;

    for (const test of tests) {
        html += renderTestRow(test, categoryKey);
    }

    html += '</div></div>';
    return html;
}

/**
 * Render a single test row with inline history and actions.
 */
function renderTestRow(test, category) {
    const shortName = test.nodeid.split('::').pop();
    const effectiveOutcome = test.retry_outcome || test.outcome;
    const outcomeIcon = effectiveOutcome === 'passed' ? '✓' : effectiveOutcome === 'skipped' ? '○' : '✗';
    const outcomeClass = effectiveOutcome === 'passed' ? 'passed' : effectiveOutcome === 'skipped' ? 'skipped' : 'failed';

    // Build history icons from recent runs
    let historyHtml = '';
    if (test.history && test.history.length > 0) {
        const icons = test.history.map(h => {
            if (h.outcome === 'passed') return '<span class="hist-icon pass">✓</span>';
            if (h.outcome === 'failed') return '<span class="hist-icon fail">✗</span>';
            return '<span class="hist-icon skip">○</span>';
        }).reverse().join('');
        historyHtml = `<span class="test-history">${icons}</span>`;
    }

    // Build flip rate indicator for flaky tests
    let flipRateHtml = '';
    if (test.flip_rate_percent && test.flip_rate_percent > 0) {
        flipRateHtml = `<span class="flip-rate">${test.flip_rate_percent}%</span>`;
    }

    // Build duration
    const durationHtml = test.duration_seconds ? `<span class="duration">${test.duration_seconds.toFixed(1)}s</span>` : '';

    // Build issue link or action buttons based on category
    let actionsHtml = '';
    if (test.existing_issue) {
        const issueNum = test.existing_issue.number;
        const issueStatus = test.existing_issue.status;
        if (category === 'fixed' && issueStatus === 'open') {
            actionsHtml = `
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}"
                   target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">
                    → #${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span>
                </a>
                <div class="test-actions">
                    <button class="action-btn success" onclick="closeE2EIssue(${issueNum}, '${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                        Close #${issueNum}
                    </button>
                </div>
            `;
        } else {
            actionsHtml = `
                <a href="https://github.com/${window.dashboardData.githubOwner}/${window.dashboardData.githubRepo}/issues/${issueNum}"
                   target="_blank" class="issue-link-inline" onclick="event.stopPropagation();">
                    → #${issueNum} <span class="issue-status ${issueStatus}">${issueStatus}</span>
                </a>
            `;
        }
    } else if (category === 'untriaged' || category === 'flaky') {
        actionsHtml = `
            <div class="test-actions">
                <button class="action-btn primary" onclick="showCreateIssueDropdown(this, '${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                    Create Issue ▼
                </button>
                <button class="action-btn warning" onclick="quarantineSingleTest('${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                    Quarantine
                </button>
                <button class="action-btn" onclick="copyTestErrorFromRun('${escapeAttr(test.nodeid)}'); event.stopPropagation();">
                    Copy Error
                </button>
            </div>
        `;
    }

    // Build the error preview (first 2 lines)
    let errorPreviewHtml = '';
    if (test.longrepr && (category === 'untriaged' || category === 'has_issue' || category === 'flaky')) {
        const lines = test.longrepr.split('\n');
        const preview = lines.slice(0, 2).join('\n');
        const hasMore = lines.length > 2;
        errorPreviewHtml = `
            <div class="test-error-preview" data-nodeid="${escapeAttr(test.nodeid)}">
                <pre class="error-text">${escapeHtml(preview)}</pre>
                ${hasMore ? `<button class="expand-btn" onclick="toggleTestError(this); event.stopPropagation();">Expand ▼</button>` : ''}
            </div>
        `;
    }

    return `
        <div class="test-row" data-nodeid="${escapeAttr(test.nodeid)}">
            <div class="test-row-main">
                <span class="status-icon ${outcomeClass}">${outcomeIcon}</span>
                <span class="test-name" title="${escapeHtml(test.nodeid)}">${escapeHtml(shortName)}</span>
                ${historyHtml}
                ${flipRateHtml}
                ${durationHtml}
                ${actionsHtml}
            </div>
            ${errorPreviewHtml}
        </div>
    `;
}

/**
 * Toggle a category section's visibility.
 */
/**
 * Switch between Tests and Timeline tabs in the E2E run detail view.
 */
function switchE2ERunTab(tabName, btn) {
    // Update tab buttons
    const tabs = document.querySelectorAll('.e2e-run-tab');
    tabs.forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');

    // Toggle panels
    const testsPanel = document.getElementById('e2eRunTestsTab');
    const timelinePanel = document.getElementById('e2eRunTimelineTab');
    if (testsPanel) testsPanel.style.display = tabName === 'tests' ? '' : 'none';
    if (timelinePanel) timelinePanel.style.display = tabName === 'timeline' ? '' : 'none';
}

/**
 * Switch timeline view (Story/Ops/Debug) and re-fetch with the selected filter.
 */
async function switchE2ETimelineView(view, btn) {
    // Update view buttons
    const btns = document.querySelectorAll('.e2e-view-btn');
    btns.forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');

    // Re-fetch timeline with the selected view
    const runId = unifiedRunData && unifiedRunData.run ? unifiedRunData.run.id : null;
    if (!runId) return;

    const container = document.getElementById('e2eTimelineContent');
    if (!container) return;
    container.innerHTML = '<div class="loading-spinner">Loading...</div>';

    try {
        const res = await fetch(`/control/e2e/run/${runId}/timeline?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}&view=${encodeURIComponent(view)}`);
        if (!res.ok) {
            container.innerHTML = '<div style="color: var(--danger);">Failed to load timeline</div>';
            return;
        }
        const tl = await res.json();
        renderTimeline(container, tl.events || [], tl.phase_toc || [], tl.cycles || []);
    } catch (err) {
        container.innerHTML = `<div style="color: var(--danger);">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function toggleCategorySection(categoryKey) {
    const testsDiv = document.getElementById(`tests-${categoryKey}`);
    const toggleSpan = document.getElementById(`toggle-${categoryKey}`);
    const section = document.querySelector(`.category-section[data-category="${categoryKey}"]`);
    const header = section?.querySelector('.category-header');
    if (!testsDiv || !toggleSpan) return;

    const isCollapsed = testsDiv.classList.contains('collapsed');
    testsDiv.classList.toggle('collapsed');
    toggleSpan.textContent = isCollapsed ? '▼' : '▶';
    if (header) {
        header.setAttribute('aria-expanded', isCollapsed ? 'true' : 'false');
    }
}

/**
 * Toggle error preview/full view.
 */
function toggleTestError(button) {
    const preview = button.closest('.test-error-preview');
    if (!preview) return;

    const isExpanded = preview.classList.contains('expanded');
    const nodeid = preview.dataset.nodeid;

    if (isExpanded) {
        // Collapse: show first 2 lines
        preview.classList.remove('expanded');
        button.textContent = 'Expand ▼';
        const errorText = preview.querySelector('.error-text');
        if (errorText && unifiedRunData) {
            // Find the test and show preview
            for (const category of Object.values(unifiedRunData.tests_by_category)) {
                const test = category.find(t => t.nodeid === nodeid);
                if (test && test.longrepr) {
                    const lines = test.longrepr.split('\n');
                    errorText.textContent = lines.slice(0, 2).join('\n');
                    break;
                }
            }
        }
    } else {
        // Expand: show full error
        preview.classList.add('expanded');
        button.textContent = 'Collapse ▲';
        const errorText = preview.querySelector('.error-text');
        if (errorText && unifiedRunData) {
            // Find the test and show full error
            for (const category of Object.values(unifiedRunData.tests_by_category)) {
                const test = category.find(t => t.nodeid === nodeid);
                if (test && test.longrepr) {
                    errorText.textContent = test.longrepr;
                    break;
                }
            }
        }
    }
}

/**
 * Copy error text for a specific test.
 */
function copyTestErrorFromRun(nodeid) {
    if (!unifiedRunData) return;

    // Find the test in any category
    for (const category of Object.values(unifiedRunData.tests_by_category)) {
        const test = category.find(t => t.nodeid === nodeid);
        if (test) {
            const text = `Test: ${test.nodeid}\n\nError:\n${test.longrepr || 'No error details'}`;
            navigator.clipboard.writeText(text).then(
                () => showToast('Error copied to clipboard'),
                () => showToast('Failed to copy', true)
            );
            return;
        }
    }
}

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

    const untriaged = unifiedRunData.tests_by_category.untriaged || [];
    if (untriaged.length === 0) {
        showToast('No untriaged tests', true);
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

/**
 * Close an E2E failure issue that has been fixed.
 */
async function closeE2EIssue(issueNumber, nodeid) {
    if (!confirm(`Close issue #${issueNumber}? The test "${nodeid.split('::').pop()}" is now passing.`)) {
        return;
    }

    try {
        const res = await fetch(`/control/e2e/close-issue/${issueNumber}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeid }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || 'Failed to close issue', true);
            return;
        }

        showToast(`Closed issue #${issueNumber}`);

        // Refresh the view
        if (unifiedRunData) {
            showUnifiedRunView(unifiedRunData.run.id);
        }
    } catch (err) {
        showToast('Failed to close issue: ' + err.message, true);
    }
}

/**
 * Show dropdown for creating a single issue with agent selection.
 */
function showCreateIssueDropdown(button, nodeid) {
    // If dropdown already exists, toggle it
    let dropdown = button.nextElementSibling;
    if (dropdown && dropdown.classList.contains('create-issue-dropdown')) {
        dropdown.remove();
        return;
    }

    // Remove any other open dropdowns
    document.querySelectorAll('.create-issue-dropdown').forEach(d => d.remove());

    // Create dropdown
    dropdown = document.createElement('div');
    dropdown.className = 'create-issue-dropdown';
    dropdown.innerHTML = `
        <div class="dropdown-content">
            ${window.dashboardData.agents.map(a => `
                <button class="dropdown-item" onclick="createSingleIssueWithAgent('${escapeAttr(nodeid)}', '${a}'); event.stopPropagation();">
                    ${a}
                </button>
            `).join('')}
        </div>
    `;
    button.parentNode.insertBefore(dropdown, button.nextSibling);

    // Close dropdown when clicking elsewhere
    const closeHandler = (e) => {
        if (!dropdown.contains(e.target) && e.target !== button) {
            dropdown.remove();
            document.removeEventListener('click', closeHandler);
        }
    };
    setTimeout(() => document.addEventListener('click', closeHandler), 0);
}

/**
 * Create a single issue with specified agent.
 */
async function createSingleIssueWithAgent(nodeid, agent) {
    if (!unifiedRunData) return;

    try {
        const res = await fetch(`/control/e2e/create-issues/${unifiedRunData.run.id}?repo_root=${encodeURIComponent(REPO_ROOT)}&config_name=${encodeURIComponent(CONFIG_NAME)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nodeids: [nodeid], agent }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.error || data.detail || 'Failed to create issue', true);
            return;
        }

        const testName = nodeid.split('::').pop();
        showToast(`Created issue #${data.parent_issue.number} for ${testName}`);

        // Close dropdown
        document.querySelectorAll('.create-issue-dropdown').forEach(d => d.remove());

        // Refresh the view
        showUnifiedRunView(unifiedRunData.run.id);

        // Open issue in new tab
        if (data.parent_issue.url) {
            window.open(data.parent_issue.url, '_blank');
        }
    } catch (err) {
        showToast('Failed to create issue: ' + err.message, true);
    }
}
