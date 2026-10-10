// The Control Center's cross-repository Tech lead page (#7763).
//
// A thin adapter over ONE contract-backed JSON view model
// (``ControlCenterTechLeadPayload`` from ``GET /api/control-center/tech-lead``)
// and ONE typed command (``TechLeadProposalCommandPayload`` POSTed to the
// repository's proposal endpoint). Payload shape is owned by the generated
// validators through ``uiContractJson``; this module only renders and wires.
// Approval policy (who may approve, what it executes) lives in the engine.
//
// Loadable under Node's test runner (module.exports) so rendering and the
// command boundary are verified as real behaviour.
(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory;
    }
    if (root) {
        root.createControlCenterTechLeadView = factory;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function createControlCenterTechLeadView(deps) {
    const { fetch } = deps;
    // The page's own encoder, safe in text AND quoted attributes (#7763
    // review r29 F2): it encodes quotes too, so a repository name or title
    // can never leave an attribute. Never the host page's text-only helper.
    const ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ENTITIES[char]);
    const uiContractJson = deps.contractJson;
    const notify = deps.notify || (() => {});
    // Told after every refresh, success or not, so every badge follows the
    // page's current answer (a command's refresh included).
    const onBadgeChange = deps.onBadgeChange || (() => {});
    const PAGE_ENDPOINT = '/api/control-center/tech-lead';
    const KIND_LABELS = {
        proposal: 'Proposal',
        merge_ready_pr: 'Merge-ready PR',
        hand_over: 'Hand-over',
        delivery_pr: 'Delivery PR',
    };
    let latest = null;
    let unavailable = false;
    let rootNode = null;

    // An unreported repository's backlog is unknown: never an all-clear then.
    function badgeText(count, unreported = 0) {
        const repos = unreported === 1 ? '1 repository' : `${unreported} repositories`;
        if (unreported) return `${count} waiting on you; ${repos} not reporting`;
        if (!count) return 'Nothing waiting on you';
        return `${count} waiting on you`;
    }

    const UNAVAILABLE_TEXT = 'Unable to check what waits on you';

    // What every badge shows, from the one place that knows whether the last
    // read succeeded (#7763 review r17 F2): an unreadable page is never an
    // all-clear, and never the previous count either. ``null`` before the
    // first read has finished: nothing is known yet.
    function badgeState() {
        if (latest) return { count: latest.waiting_count, text: badgeText(latest.waiting_count, latest.unreported_count) };
        return unavailable ? { count: 0, text: UNAVAILABLE_TEXT } : null;
    }

    function showUnavailable() {
        latest = null;
        unavailable = true;
        if (!rootNode) return;
        const place = focusedControl();
        // Nothing from the last good read stays up as if current (#7763 r27 F2).
        const unavailableNote = what => `<p class="tl-empty" role="status">Unable to check ${what} right now; it retries shortly.</p>`;
        rootNode.querySelector('#techLeadRunStrip').innerHTML = 'Unable to check the tech lead\'s latest run.';
        rootNode.querySelector('#techLeadWaitingHeading').textContent = 'Waiting on you';
        rootNode.querySelector('#techLeadWaitingList').innerHTML = unavailableNote('what waits on you');
        rootNode.querySelector('#techLeadDoingList').innerHTML = unavailableNote('what the tech lead is doing');
        rootNode.querySelector('#techLeadWatchingList').innerHTML = unavailableNote('what the tech lead is watching');
        // The focused card went with the list: focus its lane heading, as
        // paint() does for an item that is gone (#7763 review r18 F2).
        if (place) restoreFocus(place);
    }

    function commandEndpoint(repoKey) {
        return `/api/control-center/repositories/${encodeURIComponent(repoKey)}/tech-lead/proposals`;
    }

    function waitingEntries(payload) {
        const entries = [];
        for (const repo of payload.repos) {
            if (!repo.section) continue;
            for (const item of repo.section.waiting) {
                entries.push({ repo, item });
            }
        }
        // Oldest first across repositories; undated items after dated ones.
        entries.sort((a, b) => {
            const ta = a.item.waiting_since;
            const tb = b.item.waiting_since;
            if (!ta !== !tb) return ta ? -1 : 1;
            if (ta !== tb) return ta < tb ? -1 : 1;
            return a.item.number - b.item.number;
        });
        return entries;
    }

    // Every focusable control carries a ``data-focus-key`` naming its item and
    // role, unique in its lane, so a refresh can return focus to the same
    // control's replacement (#7763 review r3 F2 / r5 F3).
    function renderDetails(repo, item) {
        if (!item.details.length) return '';
        const rows = item.details.map(row =>
            `<dt>${escapeHtml(row.label)}</dt><dd>${escapeHtml(row.value)}</dd>`).join('');
        const key = `details:${repo.repo_key}:${item.number}`;
        return `<details class="tl-details"><summary data-focus-key="${escapeHtml(key)}">Details for #${escapeHtml(item.number)}</summary><dl>${rows}</dl></details>`;
    }

    // A decision's steps beyond its item (#8691): what approving executes, in
    // order, and the operator's own checklist of what io cannot do. Each list
    // is labelled by its visible heading; the checkbox glyph is decorative
    // (the heading says "by hand"), so colour or a glyph is never the signal.
    function renderSteps(repo, item) {
        const id = `tl-steps-${escapeHtml(repo.repo_key)}-${escapeHtml(item.number)}`;
        const parts = [];
        if (item.approval_steps.length) {
            const rows = item.approval_steps.map(step => `<li>${escapeHtml(step)}</li>`).join('');
            parts.push(`<p id="${id}-approval"><strong>Approving also:</strong></p><ol class="tl-steps" aria-labelledby="${id}-approval">${rows}</ol>`);
        }
        if (item.operator_steps.length) {
            const rows = item.operator_steps.map(step =>
                `<li><span class="tl-check" aria-hidden="true">&#9744;</span> ${escapeHtml(step)}</li>`).join('');
            parts.push(`<p id="${id}-operator"><strong>You do by hand (io cannot):</strong></p><ul class="tl-checklist" aria-labelledby="${id}-operator">${rows}</ul>`);
        }
        return parts.join('');
    }

    function renderActions(repo, item) {
        if (item.kind !== 'proposal') return '';
        const name = `proposal #${item.number} in ${repo.name}`;
        const attrs = decision => `data-tl-command="${escapeHtml(decision)}" data-repo-key="${escapeHtml(repo.repo_key)}" data-number="${escapeHtml(item.number)}" data-focus-key="${escapeHtml(`${decision}:${repo.repo_key}:${item.number}`)}"`;
        return `<div class="tl-actions">
            <button type="button" class="btn btn-primary" ${attrs('approve')} ${item.can_approve ? '' : 'disabled'} aria-label="Approve ${escapeHtml(name)}">Approve</button>
            <button type="button" class="btn" ${attrs('decline')} ${item.can_decline ? '' : 'disabled'} aria-label="Decline ${escapeHtml(name)}">Decline</button>
        </div>`;
    }

    function renderWaitingCard(repo, item) {
        const headingId = `tl-card-${escapeHtml(repo.repo_key)}-${escapeHtml(item.number)}`;
        const key = `${repo.repo_key}:${item.number}`;
        return `<li><article class="tl-card tl-kind-${escapeHtml(item.kind)}" tabindex="-1" aria-labelledby="${headingId}"
                data-repository="${escapeHtml(repo.section.repository)}" data-number="${escapeHtml(item.number)}" data-focus-key="${escapeHtml(`card:${key}`)}">
            <p class="tl-card-meta"><span class="tl-kind">${escapeHtml(KIND_LABELS[item.kind])}</span>
                · <span class="tl-repo">${escapeHtml(repo.name)}</span>
                ${item.waiting_since ? `· waiting since <time datetime="${escapeHtml(item.waiting_since)}">${escapeHtml(item.waiting_since)}</time>` : ''}</p>
            <h3 id="${headingId}"><a href="${escapeHtml(item.link)}" target="_blank" rel="noopener" data-focus-key="${escapeHtml(`link:${key}`)}">#${escapeHtml(item.number)} ${escapeHtml(item.title)}</a></h3>
            <p class="tl-status tl-status-${escapeHtml(item.status)}"><strong>Status:</strong> ${escapeHtml(item.status_label)}</p>
            <p><strong>Recommendation:</strong> ${escapeHtml(item.recommendation)}</p>
            <p><strong>${item.kind === 'proposal' ? 'Approving' : 'What to do'}:</strong> ${escapeHtml(item.approval_effect)}</p>
            ${renderSteps(repo, item)}
            ${renderDetails(repo, item)}
            ${renderActions(repo, item)}
        </article></li>`;
    }

    function renderDoing(payload) {
        const rows = [];
        for (const repo of payload.repos) {
            if (!repo.section) continue;
            for (const item of repo.section.doing) {
                const target = item.target_number ? ` <a href="${escapeHtml(item.link)}" target="_blank" rel="noopener" data-focus-key="${escapeHtml(`doing:${repo.repo_key}:${item.decision_id}`)}">#${escapeHtml(item.target_number)}</a>` : '';
                rows.push(`<li><span class="tl-outcome tl-outcome-${escapeHtml(item.outcome)}">${escapeHtml(item.outcome_label)}</span>
                    ${escapeHtml(item.action_label)}${target} · ${escapeHtml(repo.name)}
                    <span class="tl-when">${escapeHtml(item.at)}</span>
                    ${item.reason ? `<span class="tl-reason">${escapeHtml(item.reason)}</span>` : ''}</li>`);
            }
            for (const park of repo.section.parked) {
                const target = park.issue_number ? ` <a href="${escapeHtml(park.link)}" target="_blank" rel="noopener" data-focus-key="${escapeHtml(`parked:${repo.repo_key}:${park.action}:${park.subject}`)}">#${escapeHtml(park.issue_number)}</a>` : ` ${escapeHtml(park.subject)}`;
                rows.push(`<li class="tl-parked"><span class="tl-outcome tl-outcome-parked">Parked${park.escalated ? ', escalated' : ''}</span>
                    ${escapeHtml(park.action)}${target} · ${escapeHtml(repo.name)}
                    <span class="tl-when">since ${escapeHtml(park.parked_since)}</span>
                    <span class="tl-reason">${escapeHtml(park.reason)}</span></li>`);
            }
        }
        return rows.length
            ? `<ul class="tl-list">${rows.join('')}</ul>`
            : '<p class="tl-empty">Nothing in the last 24 hours.</p>';
    }

    function renderWatching(payload) {
        const blocks = [];
        for (const repo of payload.repos) {
            if (!repo.section) continue;
            const section = repo.section;
            const triaged = section.triaged.map(item =>
                `<li><a href="${escapeHtml(item.link)}" target="_blank" rel="noopener" data-focus-key="${escapeHtml(`triaged:${repo.repo_key}:${item.issue_number}`)}">#${escapeHtml(item.issue_number)}</a>
                 <span class="tl-triage">${escapeHtml(item.triage_label)}</span> <span class="tl-reason">${escapeHtml(item.reason)}</span></li>`).join('');
            const cases = section.case_files.map(item =>
                `<li><a href="${escapeHtml(item.link)}" target="_blank" rel="noopener" data-focus-key="${escapeHtml(`case:${repo.repo_key}:${item.issue_number}`)}">#${escapeHtml(item.issue_number)} ${escapeHtml(item.title)}</a>
                 ${item.area ? `<span class="tl-area">${escapeHtml(item.area)}</span>` : ''} · ${escapeHtml(item.comment_count)} observations</li>`).join('');
            blocks.push(`<div class="tl-watch-repo"><h3>${escapeHtml(repo.name)}</h3>
                <p><strong>Health review:</strong> ${escapeHtml(section.health_review.label)}</p>
                <h4>Triaged blocked items</h4>${triaged ? `<ul class="tl-list">${triaged}</ul>` : '<p class="tl-empty">None.</p>'}
                <h4>Pattern case files</h4>${cases ? `<ul class="tl-list">${cases}</ul>` : '<p class="tl-empty">None.</p>'}</div>`);
        }
        return blocks.length ? blocks.join('') : '<p class="tl-empty">No running engine to report on.</p>';
    }

    function renderRunStrip(payload) {
        const runs = payload.repos
            .filter(repo => repo.section && repo.section.run.has_run)
            .map(repo => `${escapeHtml(repo.name)}: ${escapeHtml(repo.section.run.phase_label)} — ${escapeHtml(repo.section.run.label)}`
                + (repo.section.run.started_at ? ` (${escapeHtml(repo.section.run.started_at)})` : ''));
        return runs.length ? `Latest tech-lead run · ${runs.join(' · ')}` : 'No tech-lead run recorded yet.';
    }

    function renderUnavailable(payload) {
        const rows = payload.repos
            .filter(repo => repo.availability !== 'available')
            .map(repo => `<li><strong>${escapeHtml(repo.name)}</strong>: ${escapeHtml(repo.detail)}</li>`);
        return rows.length ? `<ul class="tl-unavailable" aria-label="Repositories not reporting">${rows.join('')}</ul>` : '';
    }

    function renderPage(payload) {
        const waiting = waitingEntries(payload);
        // The count is the payload's: an approved proposal the engine is acting
        // on stays listed with its status, but it no longer waits on you.
        let clear = '';
        if (payload.waiting_count === 0 && payload.unreported_count > 0) {
            clear = '<p class="tl-empty">Nothing reported as waiting, but not every repository reported (see below): its backlog is unknown.</p>';
        } else if (payload.waiting_count === 0) {
            clear = '<p class="tl-empty tl-all-clear">Nothing is waiting on you. The tech lead will list proposals, merge-ready PRs and hand-overs here.</p>';
        }
        const waitingHtml = clear + (waiting.length
            ? `<ol class="tl-cards">${waiting.map(({ repo, item }) => renderWaitingCard(repo, item)).join('')}</ol>`
            : '');
        return {
            runStrip: renderRunStrip(payload),
            waiting: renderUnavailable(payload) + waitingHtml,
            waitingHeading: `Waiting on you (${payload.waiting_count})`,
            doing: renderDoing(payload),
            watching: renderWatching(payload),
        };
    }

    async function load() {
        const response = await fetch(PAGE_ENDPOINT);
        if (!response.ok) {
            throw new Error(await uiContractJson.errorMessage(response, 'Could not read the Tech lead page'));
        }
        // null: the reader already reported why. The caller (refresh) decides
        // whether this answer is still the newest one before using it.
        return uiContractJson.fromResponse(response, 'ControlCenterTechLeadPayload', PAGE_ENDPOINT);
    }

    // The one command path: typed body, response validated, failure surfaced.
    async function dispatch(repoKey, number, decision) {
        const response = await fetch(commandEndpoint(repoKey), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ proposal_issue_number: number, decision }),
        });
        const outcome = await uiContractJson.fromResponse(response, 'TechLeadProposalOutcomePayload', commandEndpoint(repoKey));
        if (outcome === null) throw new Error('The command answer was not understood');
        if (!response.ok) throw new Error(outcome.detail);
        return outcome;
    }

    // The page's lanes, each with the heading focus falls back to when the
    // focused item is gone after a refresh (#7763 review r5 F3).
    const LANES = [
        ['#techLeadWaitingList', '#techLeadWaitingHeading'],
        ['#techLeadDoingList', '#techLeadDoingHeading'],
        ['#techLeadWatchingList', '#techLeadWatchingHeading'],
    ];

    // Where keyboard focus sits inside a lane, by identity, so a refresh that
    // rebuilds the lane can put it back on the same control's replacement.
    function focusedControl() {
        const active = rootNode.ownerDocument && rootNode.ownerDocument.activeElement;
        if (!active) return null;
        for (const [listSelector, headingSelector] of LANES) {
            const list = rootNode.querySelector(listSelector);
            if (list && list.contains(active)) {
                return { listSelector, headingSelector, key: (active.dataset && active.dataset.focusKey) || '' };
            }
        }
        return null;
    }

    function restoreFocus(place) {
        const list = rootNode.querySelector(place.listSelector);
        const target = place.key
            ? [...list.querySelectorAll('[data-focus-key]')].find(node => node.dataset.focusKey === place.key)
            : null;
        // A disabled replacement (approved elsewhere meanwhile) cannot take
        // focus, so the lane heading does (#7763 review r9 F2).
        const usable = target && !target.disabled ? target : null;
        (usable || rootNode.querySelector(place.headingSelector)).focus();
    }

    function paint(payload) {
        if (!rootNode) return;
        const html = renderPage(payload);
        const place = focusedControl();
        rootNode.querySelector('#techLeadRunStrip').innerHTML = html.runStrip;
        rootNode.querySelector('#techLeadWaitingHeading').textContent = html.waitingHeading;
        rootNode.querySelector('#techLeadWaitingList').innerHTML = html.waiting;
        rootNode.querySelector('#techLeadDoingList').innerHTML = html.doing;
        rootNode.querySelector('#techLeadWatchingList').innerHTML = html.watching;
        if (place) restoreFocus(place);
    }

    // The page's refresh: a failed or off-contract read shows "unable to
    // check" rather than leaving the last answer up as if it were current.
    //
    // Polling and a command's refresh can overlap: only the NEWEST request
    // may change the page or the badges, so a slow older all-clear never
    // lands on top of a newer answer (#7763 review r17 F2, r19 F2).
    let refreshes = 0;
    async function refresh() {
        const request = ++refreshes;
        let payload = null;
        try {
            payload = await load();
        } catch (error) {
            console.error('Failed to load the Tech lead page:', error);
        }
        if (request !== refreshes) return payload;  // superseded while in flight
        if (payload === null) {
            showUnavailable();
        } else {
            latest = payload;
            unavailable = false;
            paint(payload);
        }
        onBadgeChange();
        return payload;
    }

    // A message from the embedded repo dashboard (#7763 review r5 F4): only
    // from the frame the Control Center embedded (a contextual invariant the
    // schema cannot know), and only in its generated contract's shape.
    //
    // ``undefined``: not a Tech lead message at all (the window's message
    // stream is shared), so the caller handles it otherwise. ``null``: a Tech
    // lead message refused. Otherwise the validated message, to dispatch on
    // its validated ``type`` (#7763 review r12 F3).
    function readFrameMessage(event, expectedSource) {
        const message = uiContractJson.fromUnionMember(event.data, 'TechLeadFrameMessage', 'dashboard frame message');
        if (message === undefined) return undefined;
        if (!expectedSource || event.source !== expectedSource) return null;
        return message;
    }

    function focusEntry(repository, number) {
        if (!rootNode) return false;
        const card = [...rootNode.querySelectorAll('.tl-card')]
            .find(node => node.dataset.repository === repository && Number(node.dataset.number) === Number(number));
        if (!card) return false;
        card.scrollIntoView({ block: 'center' });
        card.focus();
        return true;
    }

    function bind(node) {
        rootNode = node;
        const status = node.querySelector('#techLeadCommandStatus');
        node.addEventListener('click', async event => {
            const button = event.target.closest('[data-tl-command]');
            if (!button || button.disabled) return;
            const decision = button.dataset.tlCommand;
            const number = Number(button.dataset.number);
            if (decision === 'decline' && deps.confirm && !deps.confirm(`Decline and close proposal #${number}? Nothing will be executed.`)) {
                return;
            }
            button.disabled = true;
            try {
                const outcome = await dispatch(button.dataset.repoKey, number, decision);
                status.textContent = `#${outcome.proposal_issue_number}: ${outcome.detail}`;
                await refresh();
                node.querySelector('#techLeadWaitingHeading').focus();
            } catch (error) {
                status.textContent = `#${number}: ${error.message}`;
                notify(error.message, 'error');
                button.disabled = false;
            }
        });
    }

    return {
        badgeText,
        bind,
        commandEndpoint,
        dispatch,
        focusEntry,
        latest: () => latest,
        load,
        badgeState,
        paint,
        readFrameMessage,
        showUnavailable,
        refresh,
        renderPage,
        waitingEntries,
    };
});
