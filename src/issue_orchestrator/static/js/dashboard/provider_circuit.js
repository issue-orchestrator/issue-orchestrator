// Provider circuit-breaker outage banner + health panel (issue #5980).
//
// Reads the typed ``providerCircuit`` payload the dashboard view model
// publishes inside ``window.dashboardData`` and renders a page-level alert
// banner when any provider circuit is open. A native <details> disclosure
// lists per-provider state (Unavailable / Recovering), cooldown remaining,
// attempts, and last error so operators see *why* work is blocked without
// watching the UI during the outage.
//
// The banner is hidden whenever no circuit is open — a healthy fleet shows
// nothing. Live refresh: ``refreshViewModel`` re-invokes
// ``updateProviderCircuitBanner`` after each SSE-triggered fetch, so the
// banner appears/clears as ``provider.outage_entered`` /
// ``provider.outage_exited`` events flow.

function renderProviderCircuitEntryRow(entry) {
    const provider = escapeHtml(entry.provider || '');
    const isOpen = !!entry.is_open;
    const stateClass = isOpen ? 'open' : 'recovering';
    const statusLabel = escapeHtml(entry.status_label || (isOpen ? 'Unavailable' : 'Recovering'));

    const cells = [
        `<span class="pcircuit-provider">${provider}</span>`,
        `<span class="pcircuit-badge pcircuit-badge--${stateClass}">${statusLabel}</span>`,
    ];
    if (entry.cooldown_remaining_label) {
        cells.push(`<span class="pcircuit-cooldown">Retry in ${escapeHtml(entry.cooldown_remaining_label)}</span>`);
    }
    if (entry.next_retry_at) {
        const at = formatTimestamp(entry.next_retry_at, '');
        if (at) {
            cells.push(`<span class="pcircuit-eta">(${escapeHtml(at)})</span>`);
        }
    }
    const attempts = Number(entry.consecutive_outages) || 0;
    cells.push(`<span class="pcircuit-attempts">Attempts: ${attempts}</span>`);
    if (entry.last_error_summary) {
        cells.push(
            `<span class="pcircuit-error" title="${escapeAttr(entry.last_error_summary)}">`
            + `${escapeHtml(entry.last_error_summary)}</span>`,
        );
    }
    return `<li class="pcircuit-row pcircuit-row--${stateClass}">${cells.join('')}</li>`;
}

// Pure: returns the banner's inner HTML, or '' when nothing should show.
function renderProviderCircuitBannerHtml(circuit) {
    if (!circuit || !circuit.any_open) {
        return '';
    }
    const summary = escapeHtml(circuit.summary_text || 'Provider outage in progress.');
    const entries = Array.isArray(circuit.entries) ? circuit.entries : [];
    const rows = entries.map(renderProviderCircuitEntryRow).join('');
    const details = rows
        ? `<details class="pcircuit-details">`
            + `<summary class="pcircuit-summary-toggle">Circuit details</summary>`
            + `<ul class="pcircuit-list">${rows}</ul>`
            + `</details>`
        : '';
    return `<span class="pcircuit-icon" aria-hidden="true">&#x26A0;</span>`
        + `<div class="pcircuit-body">`
        + `<span class="pcircuit-summary">${summary}</span>`
        + details
        + `</div>`;
}

// Applies the rendered banner to the DOM, toggling visibility.
function updateProviderCircuitBanner(circuit) {
    const el = document.getElementById('providerCircuitBanner');
    if (!el) {
        return;
    }
    const html = renderProviderCircuitBannerHtml(circuit);
    if (html) {
        el.innerHTML = html;
        el.style.display = 'flex';
    } else {
        el.innerHTML = '';
        el.style.display = 'none';
    }
}

// Convenience for the load/refresh hooks: pull the payload off the shared
// dashboard data blob and render.
function renderProviderCircuitFromDashboardData() {
    const circuit = (typeof window !== 'undefined' && window.dashboardData)
        ? window.dashboardData.providerCircuit
        : null;
    updateProviderCircuitBanner(circuit);
}
