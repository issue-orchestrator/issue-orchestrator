// Inline "Agent attempts on issue #N" expander (issue #6322).
//
// The ``io.agent-context`` plugin renders this expander beneath the
// issue summary in a failure triage card.  At landing it's closed
// and carries no data — clicking the expander triggers a lazy fetch
// of ``/api/issue-detail/{issueNumber}?view=ops`` and renders the
// orchestrator's Attempts → Cycles tree inline.  No teleport, no
// drawer pop-out.  Result is cached on the <details> element so
// collapse + re-expand doesn't re-fetch.
//
// Vocabulary (locked in issue #6322):
//   * The backend payload still names the orchestrator-session
//     ``run`` (a JourneyRunPayload).  The view-model rename to
//     ``attempt`` is a follow-up; for now the rendering layer
//     translates: ``runs[N]`` → ``Attempt N``.
//   * Cycle is unchanged.
//   * Events are NOT rendered inline yet — v1 stops at Cycle.
//     Deeper drill-in is a later PR.

(function () {
    if (typeof window === 'undefined') return;

    // Module-private cache: ``{ [issueNumber]: Promise<IssueDetailPayload> }``.
    // Re-expanding an Attempt block hits the cache, never the
    // network twice for the same issue.
    const _issueDetailCache = new Map();

    function _fetchIssueDetailOnce(issueNumber) {
        if (_issueDetailCache.has(issueNumber)) {
            return _issueDetailCache.get(issueNumber);
        }
        const promise = fetch(`/api/issue-detail/${issueNumber}?view=ops`)
            .then((res) => {
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                return res.json();
            })
            .catch((err) => {
                // Cache the rejection only briefly: drop it on a
                // 5-second timer so the user can retry by closing
                // and reopening the expander.
                setTimeout(() => _issueDetailCache.delete(issueNumber), 5000);
                throw err;
            });
        _issueDetailCache.set(issueNumber, promise);
        return promise;
    }

    function _renderAttempts(detail) {
        const runs = Array.isArray(detail && detail.runs) ? detail.runs : [];
        if (runs.length === 0) {
            return '<div class="agent-context-empty">No agent attempts recorded for this issue.</div>';
        }
        // Render newest-first.  Each ``run`` becomes an Attempt.
        const reversed = [...runs].reverse();
        let html = '<div class="agent-context-attempts">';
        for (let i = 0; i < reversed.length; i++) {
            const run = reversed[i];
            const attemptIdx = reversed.length - i;  // human-readable: 1 = oldest, N = newest
            html += _renderAttemptRow(run, attemptIdx);
        }
        html += '</div>';
        return html;
    }

    function _renderAttemptRow(run, attemptIdx) {
        const cycles = Array.isArray(run && run.cycles) ? run.cycles : [];
        const outcome = String(run && run.outcome || 'unknown');
        const outcomeClass = _outcomeIconClass(outcome);
        const reworkSuffix = run && run.reset_from_scratch ? ' (reset)' : '';
        const summaryParts = [
            `${cycles.length} cycle${cycles.length === 1 ? '' : 's'}`,
            `final: ${escapeHtml(outcome)}`,
        ];
        let html = `<details class="agent-context-attempt"${cycles.length === 0 ? '' : ''}>`;
        html += `<summary><span class="caret">▸</span>`;
        html += `<span class="cvv-ico cvv-ico-${outcomeClass}">${_outcomeGlyph(outcomeClass)}</span>`;
        html += `<span class="agent-context-attempt-title">Attempt ${attemptIdx}${reworkSuffix}</span>`;
        html += `<span class="agent-context-attempt-meta">${summaryParts.join(' · ')}</span>`;
        html += `</summary>`;
        html += `<div class="agent-context-attempt-body">`;
        if (cycles.length === 0) {
            html += '<div class="agent-context-empty">No cycles in this attempt.</div>';
        } else {
            for (const cycle of cycles) {
                html += _renderCycleRow(cycle);
            }
        }
        html += '</div></details>';
        return html;
    }

    function _renderCycleRow(cycle) {
        const cycleNumber = (cycle && typeof cycle.cycle_number === 'number') ? cycle.cycle_number : '?';
        const cycleLabel = cycle && typeof cycle.cycle_label === 'string' && cycle.cycle_label
            ? String(cycle.cycle_label)
            : `Cycle ${cycleNumber}`;
        const outcome = String(cycle && cycle.outcome || 'unknown');
        const outcomeClass = _outcomeIconClass(outcome);
        const validation = cycle && cycle.validation && typeof cycle.validation === 'object'
            ? String(cycle.validation.state || '')
            : '';
        const parts = [`outcome: ${escapeHtml(outcome)}`];
        if (validation) parts.push(`validation: ${escapeHtml(validation)}`);
        return (
            `<div class="agent-context-cycle">` +
            `<span class="cvv-ico cvv-ico-${outcomeClass}">${_outcomeGlyph(outcomeClass)}</span>` +
            `<span class="agent-context-cycle-title">${escapeHtml(cycleLabel)}</span>` +
            `<span class="agent-context-cycle-meta">${parts.join(' · ')}</span>` +
            `</div>`
        );
    }

    function _outcomeIconClass(outcome) {
        const s = String(outcome || '').toLowerCase();
        if (s === 'completed' || s === 'passed') return 'passed';
        if (s === 'blocked' || s === 'failed') return 'failed';
        if (s === 'errored') return 'error';
        if (s === 'skipped') return 'skipped';
        return 'passed';  // unknown → muted-visual default; not used to mislead, just to avoid red noise
    }

    function _outcomeGlyph(cls) {
        if (cls === 'failed') return '✕';
        if (cls === 'error') return '⚠';
        if (cls === 'skipped') return '–';
        return '✓';
    }

    // Lazy-fetch handler called from the plugin's <details> toggle.
    // Reads ``data-issue-number`` and ``data-loaded``; on first
    // open, fetches + renders into the body; subsequent opens are
    // no-ops because the body is already populated.
    function _handleAgentAttemptsToggle(detailsEl) {
        if (!detailsEl || !detailsEl.open) return;
        if (detailsEl.dataset.loaded === '1') return;
        const issueNumber = Number(detailsEl.dataset.issueNumber);
        const body = detailsEl.querySelector('.agent-context-attempts-body');
        if (!body || !Number.isInteger(issueNumber) || issueNumber <= 0) return;
        detailsEl.dataset.loaded = '1';
        body.innerHTML = '<div class="agent-context-loading">Loading agent attempts…</div>';
        _fetchIssueDetailOnce(issueNumber)
            .then((detail) => {
                body.innerHTML = _renderAttempts(detail);
            })
            .catch((err) => {
                // Reset loaded flag so the user can retry by closing
                // and reopening — failed fetch is a temporary state.
                detailsEl.dataset.loaded = '';
                const message = err && err.message ? err.message : 'Unknown error';
                body.innerHTML = `<div class="agent-context-error">Failed to load: ${escapeHtml(message)}</div>`;
            });
    }

    function renderInlineAgentAttemptsExpander(issueNumber) {
        const n = Number(issueNumber);
        if (!Number.isInteger(n) || n <= 0) return '';
        return (
            `<details class="agent-context-attempts-expander" ` +
            `data-issue-number="${n}" ` +
            `data-loaded="" ` +
            `ontoggle="_handleAgentAttemptsToggle(this)">` +
            `<summary><span class="caret">▸</span>` +
            `<span class="agent-context-attempts-title">Attempts on issue #${n}</span>` +
            `</summary>` +
            `<div class="agent-context-attempts-body"></div>` +
            `</details>`
        );
    }

    // Expose the public + the lazy-fetch handler so the inline
    // ``ontoggle`` can dispatch to it.  The handler must be on
    // ``window`` because inline ``ontoggle`` evaluates in global
    // scope.
    window.renderInlineAgentAttemptsExpander = renderInlineAgentAttemptsExpander;
    window._handleAgentAttemptsToggle = _handleAgentAttemptsToggle;
    // For tests: expose the cache + fetch helper.  Underscored
    // names mark them as private to production code (tests use
    // them to spy on the fetch path).
    window._inlineAgentAttemptsCache = _issueDetailCache;
    window._fetchIssueDetailOnce = _fetchIssueDetailOnce;
})();
