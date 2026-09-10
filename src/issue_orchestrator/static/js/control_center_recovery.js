(function (root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory;
    }
    if (root) {
        root.createControlCenterRecoveryView = factory;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function createControlCenterRecoveryView(deps) {
    const { fetch, escapeHtml } = deps;
    const now = deps.now || Date.now;
    const REFRESH_INTERVAL_MS = 30000;
    const REPO_KEY = /^repo-[0-9a-f]{64}$/;
    const STATUSES = new Set([
        'available',
        'empty',
        'database_absent',
        'unreadable',
        'unsupported_schema',
    ]);
    const PRESENTATIONS = new Set(['observed', 'missing', 'replaced', 'unknown']);
    const cache = new Map();
    const inFlight = new Map();

    function object(value, label) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
            throw new Error(`${label} must be an object`);
        }
        return value;
    }

    function text(value, label) {
        if (typeof value !== 'string' || value.length === 0) {
            throw new Error(`${label} must be non-empty text`);
        }
        return value;
    }

    function string(value, label) {
        if (typeof value !== 'string') throw new Error(`${label} must be text`);
        return value;
    }

    function oneOf(value, allowed, label) {
        if (!allowed.has(value)) throw new Error(`${label} is invalid`);
        return value;
    }

    function array(value, label) {
        if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
        return value;
    }

    function validateWork(raw, kind) {
        const row = object(raw, `${kind} recovery row`);
        if (row.kind !== kind) throw new Error(`recovery row kind must be ${kind}`);
        const work = object(row.work, 'recovery work');
        const authority = object(work.authority, 'recovery authority');
        if (!Number.isInteger(authority.issue_number) || authority.issue_number < 1) {
            throw new Error('recovery issue number must be positive');
        }
        text(authority.record_id, 'recovery record id');
        text(authority.branch_name, 'recovery branch');
        text(work.state, 'recovery state');
        string(work.reason, 'recovery reason');
        if (typeof work.escrow_retained !== 'boolean') {
            throw new Error('recovery escrow flag must be boolean');
        }
        return row;
    }

    function validateGroup(raw) {
        const group = object(raw, 'recovery engine group');
        const engine = object(group.engine, 'recovery engine');
        text(engine.label, 'recovery engine label');
        text(engine.host, 'recovery engine host');
        if (engine.instance_id !== null && typeof engine.instance_id !== 'string') {
            throw new Error('recovery engine instance must be text or null');
        }
        oneOf(group.presentation, PRESENTATIONS, 'recovery engine presentation');
        text(group.presentation_message, 'recovery engine presentation message');
        const records = array(group.records, 'owned recovery records');
        if (records.length === 0) throw new Error('recovery engine group must have records');
        records.forEach(row => validateWork(row, 'owned'));
        return group;
    }

    function validatePayload(raw, expectedRepoKey) {
        const payload = object(raw, 'recovery response');
        if (!REPO_KEY.test(payload.repo_key) || payload.repo_key !== expectedRepoKey) {
            throw new Error('recovery response repository key mismatch');
        }
        oneOf(payload.status, STATUSES, 'recovery response status');
        text(payload.message, 'recovery response message');
        const groups = array(payload.engine_groups, 'recovery engine groups');
        const unowned = array(payload.unowned_records, 'unowned recovery records');
        enforceRowCardinality(ROW_CARDINALITY[payload.status], groups, unowned);
        groups.forEach(validateGroup);
        unowned.forEach(row => validateWork(row, 'unowned'));
        return payload;
    }

    const ROW_CARDINALITY = {
        available: 'required',
        empty: 'forbidden',
        database_absent: 'forbidden',
        unreadable: 'forbidden',
        unsupported_schema: 'forbidden',
    };

    function enforceRowCardinality(requirement, groups, unowned) {
        const count = groups.length + unowned.length;
        if (requirement === 'required' && count === 0) {
            throw new Error('available recovery response must contain records');
        }
        if (requirement === 'forbidden' && count !== 0) {
            throw new Error('unavailable recovery response must not contain records');
        }
    }

    async function request(repoKey) {
        const cached = cache.get(repoKey);
        if (cached && now() - cached.loadedAt < REFRESH_INTERVAL_MS) return cached;
        if (inFlight.has(repoKey)) return inFlight.get(repoKey);
        const pending = (async () => {
            try {
                if (!REPO_KEY.test(repoKey)) {
                    throw new Error('registered repository has no valid recovery key');
                }
                const response = await fetch(
                    `/api/control-center/repositories/${encodeURIComponent(repoKey)}/validated-work`,
                );
                if (!response.ok) {
                    throw new Error(`recovery request failed with HTTP ${response.status}`);
                }
                return {
                    loadedAt: now(),
                    payload: validatePayload(await response.json(), repoKey),
                    error: null,
                };
            } catch (error) {
                return {
                    loadedAt: now(),
                    payload: null,
                    error: error.message || 'Recovery status could not be loaded',
                };
            }
        })();
        inFlight.set(repoKey, pending);
        try {
            const result = await pending;
            cache.set(repoKey, result);
            return result;
        } finally {
            inFlight.delete(repoKey);
        }
    }

    async function load(repos) {
        await Promise.all(repos.map(async (repo) => {
            const result = await request(repo.repo_key);
            repo.validated_work = result.payload;
            repo.validated_work_error = result.error;
        }));
    }

    function hydrate(repos) {
        repos.forEach((repo) => {
            const cached = cache.get(repo.repo_key);
            repo.validated_work = cached?.payload || null;
            repo.validated_work_error = cached?.error || null;
        });
    }

    function capture(container) {
        const expandedKeys = [];
        let focusedKey = null;
        const activeElement = container?.ownerDocument?.activeElement || null;
        container?.querySelectorAll('details[data-recovery-repo-key]').forEach((details) => {
            const key = details.dataset.recoveryRepoKey;
            if (details.open) expandedKeys.push(key);
            if (details.querySelector('summary') === activeElement) focusedKey = key;
        });
        return { expandedKeys, focusedKey };
    }

    function restore(container, captured) {
        if (!captured) return;
        const expanded = new Set(captured.expandedKeys);
        container.querySelectorAll('details[data-recovery-repo-key]').forEach((details) => {
            const key = details.dataset.recoveryRepoKey;
            details.open = expanded.has(key);
            if (key === captured.focusedKey) details.querySelector('summary')?.focus();
        });
    }

    function renderRecord(row) {
        const { authority } = row.work;
        return `<li class="repo-recovery-record">
            <div class="repo-recovery-record-title">
                <span>Issue #${authority.issue_number}</span>
                <span class="repo-recovery-state">${escapeHtml(row.work.state)}</span>
            </div>
            <code>${escapeHtml(authority.branch_name)}</code>
            <p>${escapeHtml(row.work.reason)}</p>
        </li>`;
    }

    function renderGroup(group) {
        const instance = secondaryLabel(group.engine.label, group.engine.instance_id);
        return `<section class="repo-recovery-group">
            <h4>${escapeHtml(group.engine.label)}${instance}</h4>
            <p class="repo-recovery-presentation">
                <span class="repo-recovery-presentation-badge ${escapeHtml(group.presentation)}">${escapeHtml(group.presentation)}</span>
                ${escapeHtml(group.presentation_message)}
            </p>
            <ul>${group.records.map(renderRecord).join('')}</ul>
        </section>`;
    }

    function secondaryLabel(primary, secondary) {
        return secondary && secondary !== primary ? ` · ${escapeHtml(secondary)}` : '';
    }

    function renderStatusMessage(payload, title, className = '') {
        return `<div class="repo-recovery-status ${className}" role="status">
            <strong>${title}</strong>
            <span> ${escapeHtml(payload.message)}</span>
        </div>`;
    }

    function renderAvailable(payload) {
        const count = payload.engine_groups.reduce(
            (total, group) => total + group.records.length,
            payload.unowned_records.length,
        );
        const groups = payload.engine_groups.map(renderGroup).join('');
        const unowned = payload.unowned_records.length === 0 ? '' : `
            <section class="repo-recovery-group">
                <h4>No engine owner</h4>
                <p class="repo-recovery-presentation">These records are retained and ready for recovery ownership.</p>
                <ul>${payload.unowned_records.map(renderRecord).join('')}</ul>
            </section>`;
        return `<details class="repo-recovery" data-recovery-repo-key="${escapeHtml(payload.repo_key)}">
            <summary>
                <span>Preserved validated work</span>
                <span class="repo-recovery-count">${count}</span>
            </summary>
            <div class="repo-recovery-body">${groups}${unowned}</div>
        </details>`;
    }

    const STATUS_RENDERERS = {
        available: renderAvailable,
        empty: payload => renderStatusMessage(payload, 'Preserved work:'),
        database_absent: payload => renderStatusMessage(
            payload,
            'Preserved work database absent.',
            'unavailable',
        ),
        unreadable: payload => renderStatusMessage(
            payload,
            'Preserved work status unavailable.',
            'unavailable',
        ),
        unsupported_schema: payload => renderStatusMessage(
            payload,
            'Preserved work status unavailable.',
            'unavailable',
        ),
    };

    function render(repo) {
        if (repo.validated_work_error) {
            return `<div class="repo-recovery-status unavailable" role="status">
                <strong>Preserved work status unavailable.</strong>
                <span> ${escapeHtml(repo.validated_work_error)}</span>
            </div>`;
        }
        const payload = repo.validated_work;
        if (!payload) return '';
        return STATUS_RENDERERS[payload.status](payload);
    }

    return { capture, hydrate, load, render, restore, validatePayload };
});
