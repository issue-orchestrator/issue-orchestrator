// Inline "Agent attempts on issue #N" expander (issue #6322).
//
// The ``io.agent-context`` plugin renders this expander beneath the
// issue summary in a failure triage card.  At landing it's closed
// and carries no data — opening triggers a lazy fetch of
// ``/api/issue-detail/{issueNumber}?view=ops`` and renders the
// orchestrator's Attempts → Cycles tree inline.  No teleport, no
// drawer pop-out.  Result is cached on the <details> element so
// collapse + re-expand doesn't re-fetch.
//
// Command pattern (single owner)
// ──────────────────────────────
// The expander does NOT bind its own ``ontoggle`` handler.  Instead
// the rendered ``<details>`` carries a typed
// ``data-lifecycle-command`` JSON payload (the
// ``OpenInlineAgentAttemptsCommand`` Pydantic shape) and an
// ``ontoggle="runE2ELifecycleCommandFromToggle(this)"`` hook.  The
// shared dispatcher routes ``open_inline_agent_attempts`` to
// ``loadInlineAgentAttempts(issueNumber, triggerEl)`` defined here.
//
// That keeps "load the Attempts tree for issue #N" on the same
// single-owner pipeline as every other user-facing affordance in
// the canonical viewer.  Pure-JUnit consumers (tixmeup et al.) that
// don't bundle ``lifecycle_commands.js`` see the summary fields
// and nothing else — the plugin guards on
// ``typeof renderInlineAgentAttemptsExpander === 'function'``.
//
// Vocabulary (locked in issue #6322):
//   * The backend payload still names the orchestrator-session
//     ``run`` (a JourneyRunPayload).  The view-model rename to
//     ``attempt`` is a follow-up; for now the rendering layer
//     translates: ``runs[N]`` → ``Attempt N``.
//   * Cycle is unchanged.
//   * Events are NOT rendered inline yet — v1 stops at Cycle.

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
        // Path B (PR #6333): outcome is a typed ``OutcomeBadge``
        // ``{label, tone}``.  The projection layer owns tone
        // classification; the UI just reads ``.tone`` to pick its
        // visual treatment.  No string-matching, no green-by-default
        // for unknown labels.
        const outcome = _outcomeBadge(run && run.outcome);
        const reworkSuffix = run && run.reset_from_scratch ? ' (reset)' : '';
        const summaryParts = [
            `${cycles.length} cycle${cycles.length === 1 ? '' : 's'}`,
            `final: ${escapeHtml(outcome.label)}`,
        ];
        let html = `<details class="agent-context-attempt">`;
        html += `<summary><span class="caret">▸</span>`;
        html += `<span class="cvv-ico cvv-ico-${outcome.tone}">${_toneGlyph(outcome.tone)}</span>`;
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
        const outcome = _outcomeBadge(cycle && cycle.outcome);
        const validation = cycle && cycle.validation && typeof cycle.validation === 'object'
            ? String(cycle.validation.state || '')
            : '';
        const parts = [`outcome: ${escapeHtml(outcome.label)}`];
        if (validation) parts.push(`validation: ${escapeHtml(validation)}`);
        return (
            `<div class="agent-context-cycle">` +
            `<span class="cvv-ico cvv-ico-${outcome.tone}">${_toneGlyph(outcome.tone)}</span>` +
            `<span class="agent-context-cycle-title">${escapeHtml(cycleLabel)}</span>` +
            `<span class="agent-context-cycle-meta">${parts.join(' · ')}</span>` +
            `</div>`
        );
    }

    // Outcome reader: the backend ships ``OutcomeBadge {label, tone}``.
    // Defensive normalization for malformed/legacy shapes — never
    // silently render "?" as green.
    function _outcomeBadge(value) {
        if (value && typeof value === 'object' && typeof value.label === 'string') {
            const tone = _knownTone(value.tone) ? value.tone : 'neutral';
            return { label: value.label, tone };
        }
        // Defensive fallback for a malformed payload — neutral, not
        // passed.  Path B's whole point: unknown ≠ success.
        return { label: String(value == null ? '' : value) || 'unknown', tone: 'neutral' };
    }

    function _knownTone(t) {
        return t === 'passed' || t === 'failed' || t === 'error'
            || t === 'in_progress' || t === 'neutral';
    }

    function _toneGlyph(tone) {
        if (tone === 'failed') return '✕';
        if (tone === 'error') return '⚠';
        if (tone === 'in_progress') return '…';
        if (tone === 'neutral') return '·';
        return '✓';
    }

    // Typed-Command handler.  Dispatched by ``runE2ELifecycleCommand``
    // when ``kind === 'open_inline_agent_attempts'``.  Pre-conditions
    // (open + ``dataset.loaded !== '1'``) are enforced upstream by
    // ``runE2ELifecycleCommandFromToggle``, but we re-check
    // ``detailsEl`` defensively so a programmatic dispatcher call
    // doesn't crash on a stale element.
    function loadInlineAgentAttempts(issueNumber, detailsEl) {
        const n = Number(issueNumber);
        if (!Number.isInteger(n) || n <= 0) return;
        if (!detailsEl) return;
        const body = detailsEl.querySelector('.agent-context-attempts-body');
        if (!body) return;
        detailsEl.dataset.loaded = '1';
        body.innerHTML = '<div class="agent-context-loading">Loading agent attempts…</div>';
        _fetchIssueDetailOnce(n)
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
        // Typed Command (``OpenInlineAgentAttemptsCommand``) on the
        // ``<details>`` itself.  ``runE2ELifecycleCommandFromToggle``
        // reads ``data-lifecycle-command`` on toggle, JSON-parses,
        // and routes through the shared dispatcher.
        const command = {
            kind: 'open_inline_agent_attempts',
            label: 'Open Inline Agent Attempts',
            issue_number: n,
        };
        const payload = escapeAttr(JSON.stringify(command));
        return (
            `<details class="agent-context-attempts-expander" ` +
            `data-issue-number="${n}" ` +
            `data-loaded="" ` +
            `data-lifecycle-command="${payload}" ` +
            `ontoggle="runE2ELifecycleCommandFromToggle(this)">` +
            `<summary><span class="caret">▸</span>` +
            `<span class="agent-context-attempts-title">Attempts on issue #${n}</span>` +
            `</summary>` +
            `<div class="agent-context-attempts-body"></div>` +
            `</details>`
        );
    }

    // Expose the renderer + the typed-Command handler.  The handler
    // lives on the global window so the dispatcher's
    // ``loadInlineAgentAttempts`` reference resolves regardless of
    // bundling order — same shape as every other handler in the
    // dispatcher (``openIssueTimeline``, ``openAgentLogAction``, …).
    window.renderInlineAgentAttemptsExpander = renderInlineAgentAttemptsExpander;
    window.loadInlineAgentAttempts = loadInlineAgentAttempts;
    // PR #6333 reviewer feedback: do NOT publish the fetch helper
    // or the module-private cache on ``window``.  Earlier drafts
    // exposed them as test-only globals; in practice no test uses
    // them, and test-only globals expand the UI's public API
    // surface for no value.  Tests drive the typed-Command
    // dispatcher (``runE2ELifecycleCommandFromToggle``) instead.
})();
