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
// ``ontoggle="runLifecycleCommandFromToggle(this)"`` hook.  The
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
// The Attempts → Cycles → Events body uses ``renderIssueLifecycleTimeline``
// from the ``io.agent-context`` plugin.  The dashboard issue drawer uses the
// same plugin renderer for its primary timeline, so the E2E plugin and
// dashboard timeline share the run/cycle/event display instead of drifting.

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

    function _renderAttempts(detail, issueNumber) {
        const runs = Array.isArray(detail && detail.runs) ? detail.runs : [];
        if (runs.length === 0) {
            return '<div class="agent-context-empty">No agent attempts recorded for this issue.</div>';
        }
        if (typeof renderIssueLifecycleTimeline !== 'function') {
            return '<div class="agent-context-error">Issue lifecycle renderer is unavailable.</div>';
        }
        // Render newest-first.  Each ``run`` becomes an Attempt.
        const reversed = [...runs].reverse();
        return renderIssueLifecycleTimeline(reversed, {
            baseId: `agent-context-issue-${issueNumber}`,
            issueNumber,
            listClassName: 'agent-context-attempts',
            showToneIcon: true,
            runLabel: (run, ctx) => {
                const attemptIdx = reversed.length - ctx.runIndex;  // human-readable: 1 = oldest, N = newest
                const resetSuffix = run && run.reset_from_scratch ? ' (reset)' : '';
                return `Attempt ${attemptIdx}${resetSuffix}`;
            },
            runMeta: (run) => {
                const cycles = Array.isArray(run && run.cycles) ? run.cycles : [];
                return `<span class="agent-context-attempt-meta">${cycles.length} cycle${cycles.length === 1 ? '' : 's'}</span>`;
            },
            renderCycleSummaryExtras: (cycle) => {
                const validation = cycle && cycle.validation && typeof cycle.validation === 'object'
                    ? String(cycle.validation.state || '')
                    : '';
                return validation
                    ? `<span class="agent-context-cycle-meta">validation: ${escapeHtml(validation)}</span>`
                    : '';
            },
        });
    }

    // Typed-Command handler.  Dispatched by ``runLifecycleCommand``
    // when ``kind === 'open_inline_agent_attempts'``.  Pre-conditions
    // (open + ``dataset.loaded !== '1'``) are enforced upstream by
    // ``runLifecycleCommandFromToggle``, but we re-check
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
                body.innerHTML = _renderAttempts(detail, n);
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
        // ``<details>`` itself.  ``runLifecycleCommandFromToggle``
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
            `ontoggle="runLifecycleCommandFromToggle(this)">` +
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
    // dispatcher (``runLifecycleCommandFromToggle``) instead.
})();
