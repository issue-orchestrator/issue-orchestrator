(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory;
    if (root) root.createControlCenterRecoveryStopView = factory;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createControlCenterRecoveryStopView(deps) {
    const { document, escapeHtml, fetch } = deps;
    const notify = deps.notify || (() => {});
    const refresh = deps.refresh || (async () => {});
    const navigateToEngineControls = deps.navigateToEngineControls || (() => {});
    const STOP_AVAILABILITIES = new Set([
        'available',
        'remote_host',
        'exact_target_unavailable',
    ]);
    const STOP_STATUSES = new Set([
        'stopped',
        'no_such_record',
        'record_unavailable',
        'not_owned',
        'owner_changed',
        'stop_in_progress',
        'repo_mismatch',
        'remote_host',
        'stop_failed',
    ]);
    const OUTCOME_OWNER_REQUIREMENT = {
        stopped: 'required',
        no_such_record: 'forbidden',
        record_unavailable: 'forbidden',
        not_owned: 'forbidden',
        owner_changed: 'optional',
        stop_in_progress: 'required',
        repo_mismatch: 'optional',
        remote_host: 'required',
        stop_failed: 'required',
    };
    let bound = false;
    let pending = null;
    let opener = null;

    function requireValue(condition, message) {
        if (!condition) throw new Error(message);
    }

    function object(value, label) {
        requireValue(value && typeof value === 'object' && !Array.isArray(value), `${label} must be an object`);
        return value;
    }

    function text(value, label) {
        requireValue(typeof value === 'string' && value.length > 0, `${label} must be non-empty text`);
        return value;
    }

    function positiveInteger(value, label) {
        requireValue(Number.isInteger(value) && value > 0, `${label} must be positive`);
        return value;
    }

    function positiveNumber(value, label) {
        requireValue(typeof value === 'number' && Number.isFinite(value) && value > 0, `${label} must be positive`);
        return value;
    }

    function validateEngine(raw) {
        const engine = object(raw, 'recovery engine');
        text(engine.repo_root, 'recovery engine repository');
        requireValue(
            engine.instance_id === null || (typeof engine.instance_id === 'string' && engine.instance_id.length > 0),
            'recovery engine instance must be non-empty text or null',
        );
        text(engine.host, 'recovery engine host');
        text(engine.label, 'recovery engine label');
        const process = object(engine.process, 'recovery engine process');
        text(process.host, 'recovery engine process host');
        positiveInteger(process.pid, 'recovery engine process pid');
        text(process.started_at, 'recovery engine process start');
        requireValue(
            process.instance_id === null || (typeof process.instance_id === 'string' && process.instance_id.length > 0),
            'recovery process instance must be non-empty text or null',
        );
        requireValue(
            process.host === engine.host && process.instance_id === engine.instance_id,
            'recovery engine and process identity disagree',
        );
        return engine;
    }

    function engineIdentity(engine) {
        return JSON.stringify([
            engine.repo_root,
            engine.instance_id,
            engine.host,
            engine.label,
            engine.process.host,
            engine.process.pid,
            engine.process.started_at,
            engine.process.instance_id,
        ]);
    }

    function validateAction(raw, expectedRecordId, expectedEngine, expectedFence) {
        const action = object(raw, 'recovery stop action');
        text(action.record_id, 'recovery stop record id');
        const engine = validateEngine(action.expected_engine);
        positiveInteger(action.expected_owner_fence, 'recovery stop owner fence');
        positiveNumber(action.graceful_timeout_seconds, 'recovery stop graceful timeout');
        requireValue(typeof action.force_on_timeout === 'boolean', 'recovery stop force policy must be boolean');
        requireValue(action.record_id === expectedRecordId, 'recovery stop record changed');
        requireValue(engineIdentity(engine) === engineIdentity(expectedEngine), 'recovery stop engine changed');
        requireValue(action.expected_owner_fence === expectedFence, 'recovery stop fence changed');
        return action;
    }

    function validateOwner(raw) {
        const owner = object(raw, 'recovery owner');
        const engine = validateEngine(owner.engine);
        positiveInteger(owner.owner_fence, 'recovery owner fence');
        requireValue(STOP_AVAILABILITIES.has(owner.stop_availability), 'recovery stop availability is invalid');
        return { engine, owner };
    }

    function validateRow(row, groupEngine) {
        const { engine, owner } = validateOwner(row.owner);
        requireValue(engineIdentity(engine) === engineIdentity(groupEngine), 'recovery group and owner engine disagree');
        const available = owner.stop_availability === 'available';
        requireValue(available === (row.stop_action !== null), 'recovery stop action does not match availability');
        if (available) {
            validateAction(
                row.stop_action,
                row.work.authority.record_id,
                engine,
                owner.owner_fence,
            );
        }
        return row;
    }

    function validateOutcomeOwner(outcome) {
        requireValue(Object.hasOwn(outcome, 'observed_owner'), 'recovery stop response owner is required');
        const requirement = OUTCOME_OWNER_REQUIREMENT[outcome.status];
        requireValue(requirement !== 'required' || outcome.observed_owner !== null, 'recovery stop response owner is missing');
        requireValue(requirement !== 'forbidden' || outcome.observed_owner === null, 'recovery stop response owner is unexpected');
        if (outcome.observed_owner !== null) validateOwner(outcome.observed_owner);
    }

    function instanceKey(engine) {
        if (engine.instance_id === null) return 'default';
        requireValue(engine.instance_id !== 'default', 'default is a reserved engine instance key');
        requireValue(!engine.instance_id.includes('/'), 'engine instance key must be one path component');
        return engine.instance_id;
    }

    function renderAction(row, repoKey) {
        validateRow(row, row.owner.engine);
        const owner = row.owner;
        if (owner.stop_availability === 'remote_host') {
            return `<p class="repo-recovery-stop-note">Stop unavailable here: this engine runs on ${escapeHtml(owner.engine.host)}.</p>`;
        }
        if (owner.stop_availability === 'exact_target_unavailable') {
            return `<div class="repo-recovery-stop-note">
                <p>Exact engine stop is unavailable here. Use the repository's independent engine controls.</p>
                <button type="button" class="btn btn-sm repo-recovery-engine-controls"
                    data-recovery-engine-controls="${escapeHtml(repoKey)}"
                    data-recovery-focus-key="engine-controls:${escapeHtml(row.work.authority.record_id)}"
                    aria-label="Go to independent engine controls for ${escapeHtml(owner.engine.label)}">
                    Go to engine controls
                </button>
            </div>`;
        }
        const action = row.stop_action;
        const issue = row.work.authority.issue_number;
        const encoded = escapeHtml(JSON.stringify(action));
        return `<button type="button" class="btn btn-danger repo-recovery-stop"
            data-recovery-stop-action="${encoded}"
            data-recovery-stop-repo-key="${escapeHtml(repoKey)}"
            data-recovery-stop-instance-key="${escapeHtml(instanceKey(action.expected_engine))}"
            data-recovery-focus-key="stop:${escapeHtml(action.record_id)}"
            aria-label="Stop engine ${escapeHtml(action.expected_engine.label)} owning retained work for issue ${issue}">
            Stop engine
        </button>`;
    }

    function elements() {
        return {
            dialog: document.getElementById('recoveryStopDialog'),
            form: document.getElementById('recoveryStopForm'),
            close: document.getElementById('closeRecoveryStopDialog'),
            cancel: document.getElementById('cancelRecoveryStop'),
            confirm: document.getElementById('confirmRecoveryStop'),
            summary: document.getElementById('recoveryStopSummary'),
            reason: document.getElementById('recoveryStopReason'),
            status: document.getElementById('recoveryStopStatus'),
        };
    }

    function requireElements(ui) {
        requireValue(Object.values(ui).every(Boolean), 'recovery stop dialog is incomplete');
        return ui;
    }

    function showStatus(ui, message, kind = 'error') {
        ui.status.hidden = false;
        ui.status.className = `recovery-stop-status ${kind}`;
        ui.status.textContent = message;
    }

    function setBusy(ui, busy) {
        ui.dialog.setAttribute('aria-busy', String(busy));
        ui.reason.readOnly = busy;
        ui.close.disabled = busy;
        ui.cancel.disabled = busy;
        ui.confirm.disabled = busy;
        ui.confirm.textContent = busy ? 'Stopping…' : 'Stop engine';
        if (busy) ui.reason.focus();
    }

    function open(button) {
        const ui = requireElements(elements());
        const action = JSON.parse(button.dataset.recoveryStopAction);
        validateAction(
            action,
            action.record_id,
            validateEngine(action.expected_engine),
            action.expected_owner_fence,
        );
        requireValue(button.dataset.recoveryStopInstanceKey === instanceKey(action.expected_engine), 'recovery stop route changed');
        pending = {
            action,
            instanceKey: button.dataset.recoveryStopInstanceKey,
            repoKey: button.dataset.recoveryStopRepoKey,
        };
        opener = button;
        const forcePolicy = action.force_on_timeout
            ? 'then force-stops it if it has not exited'
            : 'and does not force it if the timeout expires';
        ui.summary.textContent = `Stopping ${action.expected_engine.label} stops every job that engine is running, not only the job associated with record ${action.record_id}. It first requests graceful shutdown and waits up to ${action.graceful_timeout_seconds} seconds, ${forcePolicy}.`;
        ui.reason.value = '';
        ui.status.hidden = true;
        setBusy(ui, false);
        ui.dialog.showModal();
        ui.reason.focus();
    }

    function restoreFocus() {
        const repoKey = pending?.repoKey;
        if (opener?.isConnected) opener.focus();
        else if (repoKey) document.querySelector(`details[data-recovery-repo-key="${repoKey}"] summary`)?.focus();
        pending = null;
        opener = null;
    }

    function close() {
        const ui = requireElements(elements());
        if (ui.dialog.getAttribute('aria-busy') === 'true') return;
        ui.dialog.close();
    }

    function keepFocusInDialog(event, dialog) {
        if (event.key !== 'Tab') return;
        const controls = [...dialog.querySelectorAll('button:not(:disabled), textarea:not(:disabled)')];
        const first = controls[0];
        const last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    async function requestStop(command, reason) {
        const body = {
            record_id: command.action.record_id,
            expected_engine: command.action.expected_engine,
            expected_owner_fence: command.action.expected_owner_fence,
            reason,
        };
        const response = await fetch(
            `/api/control-center/repositories/${encodeURIComponent(command.repoKey)}`
            + `/engines/${encodeURIComponent(command.instanceKey)}/stop-validated-work-owner`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            },
        );
        const outcome = object(await response.json(), 'recovery stop response');
        requireValue(STOP_STATUSES.has(outcome.status), 'recovery stop response status is invalid');
        text(outcome.message, 'recovery stop response message');
        validateOutcomeOwner(outcome);
        requireValue(response.ok === (outcome.status === 'stopped'), 'recovery stop HTTP status disagrees with outcome');
        return outcome;
    }

    async function submit(event) {
        event.preventDefault();
        const ui = requireElements(elements());
        const reason = ui.reason.value.trim();
        if (!reason) {
            showStatus(ui, 'Enter a reason before stopping this Repository Engine.');
            ui.reason.focus();
            return;
        }
        requireValue(pending !== null, 'recovery stop dialog has no command');
        setBusy(ui, true);
        try {
            const outcome = await requestStop(pending, reason);
            if (outcome.status === 'stopped') {
                notify(outcome.message, 'success');
                await refresh(pending.repoKey);
                ui.dialog.close();
                return;
            }
            showStatus(ui, outcome.message);
            await refresh(pending.repoKey);
        } catch (error) {
            showStatus(ui, error.message || 'The exact engine stop failed.');
        } finally {
            setBusy(ui, false);
        }
    }

    function bind(container) {
        if (bound) return;
        const ui = requireElements(elements());
        container.addEventListener('click', (event) => {
            const navigation = event.target.closest('[data-recovery-engine-controls]');
            if (navigation) {
                navigateToEngineControls(navigation.dataset.recoveryEngineControls);
                return;
            }
            const button = event.target.closest('[data-recovery-stop-action]');
            if (button) open(button);
        });
        ui.form.addEventListener('submit', submit);
        ui.close.addEventListener('click', close);
        ui.cancel.addEventListener('click', close);
        ui.dialog.addEventListener('cancel', (event) => {
            if (ui.dialog.getAttribute('aria-busy') === 'true') event.preventDefault();
        });
        ui.dialog.addEventListener('keydown', (event) => keepFocusInDialog(event, ui.dialog));
        ui.dialog.addEventListener('close', restoreFocus);
        bound = true;
    }

    return { bind, renderAction, requestStop, validateRow };
});
