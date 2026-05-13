// Inline E2E runs-as-rows list.
//
// The dashboard's "Run History" panel renders one ``<details>`` row
// per E2E run.  Closed by default; expanding lazy-fetches
// ``/api/e2e-run-detail/{run_id}`` and mounts the canonical viewer
// inline.
//
// Typed-Command contract: each row carries an ``ExpandE2ERunCommand``
// in ``data-lifecycle-command`` and an
// ``ontoggle="runE2ELifecycleCommandFromToggle(this)"`` hook; the
// shared dispatcher routes ``expand_e2e_run`` →
// ``loadE2ERunIntoRow``.  ``open_e2e_run`` (from chips elsewhere on
// the dashboard) re-routes to ``expandE2ERunRow``, which scrolls to
// and opens the matching row.
//
// The runs list itself is eager: ``renderE2ERunsList`` mounts on
// ``DOMContentLoaded`` from inline JSON at ``#recentE2ERunsData``.

(function () {
    if (typeof window === 'undefined') return;

    // ── Tone helpers ─────────────────────────────────────────────
    // OutcomeBadge is owned by the projection (PR #6333) — the UI
    // reads ``.tone`` directly.  Unknown tones fall back to neutral,
    // never passed (the silent-green bug OutcomeBadge prevents).

    const _KNOWN_TONES = new Set(['passed', 'failed', 'error', 'in_progress', 'neutral']);

    function _toneFor(badge) {
        if (badge && typeof badge === 'object' && _KNOWN_TONES.has(badge.tone)) {
            return badge.tone;
        }
        return 'neutral';
    }

    function _toneGlyph(tone) {
        if (tone === 'failed') return '✕';
        if (tone === 'error') return '⚠';
        if (tone === 'in_progress') return '⟳';
        if (tone === 'neutral') return '·';
        return '✓';
    }

    function _toneClass(tone) {
        return `e2e-run-row-${tone}`;
    }

    // ── Count spans ──────────────────────────────────────────────
    // "1 failed · 1 errored · 36 passed · 2 skipped".  Each non-zero
    // count gets its own ``<span class="e2e-run-count e2e-run-count-<tone>">``
    // so CSS can color failed/errored red, passed green, etc.

    function _renderCountSpans(results) {
        const r = results || {};
        const parts = [];
        const failed = Number(r.failed) || 0;
        const errored = Number(r.errored) || 0;
        const passed = Number(r.passed) || 0;
        const skipped = Number(r.skipped) || 0;
        const quarantined = Number(r.quarantined) || 0;
        if (failed > 0)      parts.push(`<span class="e2e-run-count e2e-run-count-failed">${failed} failed</span>`);
        if (errored > 0)     parts.push(`<span class="e2e-run-count e2e-run-count-error">${errored} errored</span>`);
        if (passed > 0)      parts.push(`<span class="e2e-run-count e2e-run-count-passed">${passed} passed</span>`);
        if (skipped > 0)     parts.push(`<span class="e2e-run-count e2e-run-count-neutral">${skipped} skipped</span>`);
        if (quarantined > 0) parts.push(`<span class="e2e-run-count e2e-run-count-neutral">${quarantined} quarantined</span>`);
        if (parts.length === 0) {
            parts.push('<span class="e2e-run-count e2e-run-count-neutral">no test results</span>');
        }
        return parts.join(' · ');
    }

    // ── Meta line ────────────────────────────────────────────────
    // Compact secondary line: commit · branch · duration · started_at.

    function _formatRelative(timestamp) {
        if (!timestamp) return '';
        try {
            const dt = new Date(String(timestamp).replace(' ', 'T'));
            if (!Number.isFinite(dt.getTime())) return '';
            const now = new Date();
            const delta = Math.max(0, (now - dt) / 1000);
            if (delta < 60) return 'just now';
            if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
            if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
            return `${Math.floor(delta / 86400)}d ago`;
        } catch (_) {
            return '';
        }
    }

    function _renderMeta(summary) {
        const meta = [];
        if (summary.commit_sha) {
            const sha = String(summary.commit_sha).slice(0, 7);
            meta.push(`<code class="e2e-run-meta-sha">${escapeHtml(sha)}</code>`);
        }
        if (summary.branch) {
            meta.push(`<span class="e2e-run-meta-branch">${escapeHtml(summary.branch)}</span>`);
        }
        if (typeof summary.duration_seconds === 'number' && Number.isFinite(summary.duration_seconds)) {
            meta.push(`<span class="e2e-run-meta-duration">${summary.duration_seconds.toFixed(1)}s</span>`);
        }
        const rel = _formatRelative(summary.started_at);
        if (rel) meta.push(`<span class="e2e-run-meta-time">${escapeHtml(rel)}</span>`);
        return meta.join(' · ');
    }

    // ── Row + list renderers ─────────────────────────────────────

    function renderE2ERunsList(payload) {
        const runs = (payload && Array.isArray(payload.runs)) ? payload.runs : [];
        if (runs.length === 0) {
            return '<div class="e2e-runs-list-empty">No E2E run history.</div>';
        }
        const rows = runs.map((summary) => renderE2ERunRow(summary)).join('');
        return `<div class="e2e-runs-list" role="list">${rows}</div>`;
    }

    function renderE2ERunRow(summary) {
        const runId = Number(summary && summary.run_id);
        if (!Number.isInteger(runId) || runId <= 0) return '';
        // ``expand_command`` is the typed Command we hand to the
        // dispatcher when the row toggles open.  The summary's own
        // validator guarantees ``expand_command.run_id === run_id``,
        // so we trust the payload over re-constructing here.
        const command = (summary && summary.expand_command) || {
            kind: 'expand_e2e_run',
            label: 'Expand E2E Run',
            run_id: runId,
        };
        const payloadAttr = escapeAttr(JSON.stringify(command));
        const tone = _toneFor(summary.outcome);
        const outcomeLabel = (summary.outcome && summary.outcome.label) || 'Unknown';
        const counts = _renderCountSpans(summary.results);
        const meta = _renderMeta(summary);
        const summaryId = `e2e-run-row-summary-${runId}`;
        const contentId = `e2e-run-row-content-${runId}`;
        const note = summary.note
            ? `<div class="e2e-run-row-note">${escapeHtml(summary.note)}</div>`
            : '';

        return (
            `<details class="e2e-run-row ${_toneClass(tone)}" ` +
            `role="listitem" ` +
            `data-e2e-run-id="${runId}" ` +
            `data-loaded="" ` +
            `data-lifecycle-command="${payloadAttr}" ` +
            `ontoggle="runE2ELifecycleCommandFromToggle(this)">` +
            `<summary class="e2e-run-row-summary" id="${summaryId}" aria-controls="${contentId}">` +
                `<span class="e2e-run-row-caret" aria-hidden="true">▸</span>` +
                `<span class="cvv-ico cvv-ico-${tone}" aria-hidden="true">${_toneGlyph(tone)}</span>` +
                `<span class="e2e-run-row-id">Run #${runId}</span>` +
                `<span class="e2e-run-row-outcome e2e-run-row-outcome-${tone}">${escapeHtml(outcomeLabel)}</span>` +
                `<span class="e2e-run-row-counts">${counts}</span>` +
                (meta ? `<span class="e2e-run-row-meta">${meta}</span>` : '') +
            `</summary>` +
            `<div class="e2e-run-row-body" id="${contentId}" role="region" aria-labelledby="${summaryId}">` +
                `${note}<div class="e2e-run-row-content"></div>` +
            `</div>` +
            `</details>`
        );
    }

    // ── Lazy detail loader ───────────────────────────────────────
    // Dispatched by ``runE2ELifecycleCommand`` when the row toggles
    // open the first time.  Re-opens are guarded upstream by
    // ``runE2ELifecycleCommandFromToggle`` (``dataset.loaded === '1'``
    // bypasses the dispatcher).

    async function loadE2ERunIntoRow(runId, detailsEl) {
        const n = Number(runId);
        if (!Number.isInteger(n) || n <= 0) return;
        if (!detailsEl) return;
        const body = detailsEl.querySelector('.e2e-run-row-content');
        if (!body) return;
        detailsEl.dataset.loaded = '1';
        body.innerHTML = '<div class="loading-spinner" role="status" aria-live="polite">Loading run details…</div>';
        try {
            const res = await fetch(`/api/e2e-run-detail/${n}?view=user`);
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                const message = (data && (data.error || data.detail)) || `HTTP ${res.status}`;
                throw new Error(String(message));
            }
            // Mount the canonical viewer body (same renderer the
            // legacy modal used) inline.  Re-uses every helper from
            // ``e2e_run_view.js`` — single owner for "render an E2E
            // run".
            if (typeof renderE2EResultsPanel !== 'function') {
                body.innerHTML = '<div class="e2e-run-row-error">Run viewer is unavailable.</div>';
                return;
            }
            body.innerHTML = `<div class="e2e-canonical-host" data-e2e-run-id="${n}">${renderE2EResultsPanel(data)}</div>`;

            const cvvRoot = body.querySelector('.cvv-root');
            if (cvvRoot && typeof enhanceCanonicalValidationViewerAccessibility === 'function') {
                enhanceCanonicalValidationViewerAccessibility(cvvRoot);
            }
            // Row-scoped class — every action mounted inside the row
            // queries within ``row`` so two expanded rows don't
            // collide.
            const timelineContainer = body.querySelector('.e2e-timeline-content');
            if (timelineContainer && typeof renderE2ETimeline === 'function') {
                const normalized = (typeof normalizeE2ETimelineData === 'function')
                    ? normalizeE2ETimelineData(data)
                    : data;
                renderE2ETimeline(timelineContainer, normalized);
            }
            // ``row._e2eRunData`` is the single source of truth for
            // the run mounted in this row.  Row-scoped actions read
            // it via ``resolveRowCommandContext`` — there is no
            // module- or window-level shared run state.
            detailsEl._e2eRunData = data;
        } catch (err) {
            detailsEl.dataset.loaded = '';
            const message = err && err.message ? err.message : 'Unknown error';
            body.innerHTML = `<div class="e2e-run-row-error">Failed to load run details: ${escapeHtml(message)}</div>`;
        }
    }

    // ── Expand-the-row entry point ───────────────────────────────
    // Replaces the legacy ``showUnifiedRunView(runId)`` modal call.
    // The dispatcher routes ``open_e2e_run`` here so chip clicks,
    // "View Results" buttons, and ``openE2ERunTimeline`` all converge
    // on one entry point — single owner for "navigate to run #N".

    function expandE2ERunRow(runId, options) {
        options = options || {};
        const n = Number(runId);
        if (!Number.isInteger(n) || n <= 0) return false;
        const row = document.querySelector(`details.e2e-run-row[data-e2e-run-id="${n}"]`);
        if (!row) {
            showToast(`Run #${n} is not in the recent runs list.`, 'warning');
            return false;
        }
        // Setting ``.open = true`` fires the ``ontoggle`` handler,
        // which routes through the dispatcher to
        // ``loadE2ERunIntoRow`` — single owner for "load + render".
        if (!row.open) row.open = true;
        try {
            row.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (_) {
            row.scrollIntoView();
        }
        if (options.expandRunDetails) {
            // The canonical viewer mounts the row's
            // ``.run-details-disclosure`` lazily; poll briefly for
            // it before flipping ``.open``.
            const start = Date.now();
            const tick = () => {
                const disclosure = row.querySelector('.run-details-disclosure');
                if (disclosure) {
                    disclosure.open = true;
                    return;
                }
                if (Date.now() - start < 5000) setTimeout(tick, 50);
            };
            tick();
        }
        return true;
    }

    // ── Initial render ───────────────────────────────────────────
    // The template embeds the typed payload as inline JSON so the
    // first paint has zero round-trips.  ``/api/e2e-runs/recent`` is
    // for refresh / external consumers (and is what the JS-vm
    // dispatch tests hit).

    function _mountInitial() {
        const root = document.getElementById('e2eRunsListRoot');
        if (!root) return;
        const dataNode = document.getElementById('recentE2ERunsData');
        if (!dataNode) {
            // No SSR data — fall back to the endpoint.
            refreshE2ERunsList().catch(() => {
                root.innerHTML = '<div class="e2e-runs-list-empty">No E2E run history.</div>';
            });
            return;
        }
        let payload = null;
        try {
            payload = JSON.parse(dataNode.textContent || '{}');
        } catch (_) {
            payload = { runs: [] };
        }
        root.innerHTML = renderE2ERunsList(payload);
    }

    async function refreshE2ERunsList(limit) {
        const root = document.getElementById('e2eRunsListRoot');
        if (!root) return;
        const query = limit ? `?limit=${encodeURIComponent(limit)}` : '';
        const res = await fetch(`/api/e2e-runs/recent${query}`);
        const payload = await res.json().catch(() => ({ runs: [] }));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        root.innerHTML = renderE2ERunsList(payload);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _mountInitial);
    } else {
        _mountInitial();
    }

    // Expose the renderer + dispatcher handlers.  The dispatcher in
    // ``lifecycle_commands.js`` resolves ``loadE2ERunIntoRow`` /
    // ``expandE2ERunRow`` off the global at click time, mirroring
    // the shape every other handler uses.
    window.renderE2ERunsList = renderE2ERunsList;
    window.renderE2ERunRow = renderE2ERunRow;
    window.loadE2ERunIntoRow = loadE2ERunIntoRow;
    window.expandE2ERunRow = expandE2ERunRow;
    window.refreshE2ERunsList = refreshE2ERunsList;
})();
