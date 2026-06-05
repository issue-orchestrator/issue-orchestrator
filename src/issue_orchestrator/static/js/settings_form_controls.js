(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.settingsFormControls = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    // Form-control encoding for the settings page.
    //
    // The closed set of control kinds is owned by
    // infra/settings_schema_support.py::classify_form_control(); the server
    // stamps each control with data-type=<kind>. This module is a dumb
    // dispatch over those tokens - it must never re-interpret the JSON
    // schema. An unknown token throws (fail-fast) instead of degrading to
    // a string value that strict POST validation would reject.

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // --- dict_enum helpers -------------------------------------------------

    function dictFieldTitle(editorEl) {
        return editorEl.dataset.fieldTitle || editorEl.dataset.field;
    }

    function dictValueOptions(editorEl) {
        return JSON.parse(editorEl.dataset.valueOptions);
    }

    function renderDictRowHtml(key, value, valueOptions, fieldTitle) {
        const options = valueOptions
            .map(
                (opt) =>
                    `<option value="${escapeHtml(opt)}"${opt === value ? ' selected' : ''}>${escapeHtml(opt)}</option>`,
            )
            .join('');
        return (
            '<div class="dict-row" role="group">'
            + `<input type="text" class="dict-key" value="${escapeHtml(key)}"`
            + ` aria-label="${escapeHtml(fieldTitle)}: key">`
            + `<select class="dict-value" aria-label="${escapeHtml(fieldTitle)}: value">${options}</select>`
            + '<button type="button" class="btn btn-secondary dict-remove">Remove</button>'
            + '</div>'
        );
    }

    function refreshDictRowA11y(rowEl, fieldTitle) {
        const key = rowEl.querySelector('.dict-key').value.trim();
        const rowName = key || 'new entry';
        rowEl.querySelector('.dict-value').setAttribute(
            'aria-label',
            `${fieldTitle}: value for ${rowName}`,
        );
        rowEl.querySelector('.dict-remove').setAttribute(
            'aria-label',
            `Remove ${fieldTitle} entry ${rowName}`,
        );
    }

    // Pure: entries [{key, value}] -> {value: object, problems: [string]}.
    // Empty and duplicate keys are reported, never silently dropped or
    // last-wins merged.
    function collectDictEntries(entries, fieldTitle) {
        const value = {};
        const problems = [];
        const seen = new Set();
        entries.forEach((entry, index) => {
            const key = entry.key.trim();
            if (!key) {
                problems.push(
                    `${fieldTitle}: row ${index + 1} has an empty key - fill it in or remove the row`,
                );
                return;
            }
            if (seen.has(key)) {
                problems.push(`${fieldTitle}: duplicate key "${key}"`);
                return;
            }
            seen.add(key);
            value[key] = entry.value;
        });
        return { value, problems };
    }

    function readDictEntries(editorEl) {
        return Array.from(editorEl.querySelectorAll('.dict-row')).map((row) => ({
            key: row.querySelector('.dict-key').value,
            value: row.querySelector('.dict-value').value,
        }));
    }

    function collectDictEditor(editorEl) {
        return collectDictEntries(readDictEntries(editorEl), dictFieldTitle(editorEl));
    }

    function setDictEditorValue(editorEl, obj) {
        const fieldTitle = dictFieldTitle(editorEl);
        const valueOptions = dictValueOptions(editorEl);
        const rowsEl = editorEl.querySelector('.dict-rows');
        rowsEl.innerHTML = Object.entries(obj || {})
            .map(([key, value]) => renderDictRowHtml(key, value, valueOptions, fieldTitle))
            .join('');
        rowsEl.querySelectorAll('.dict-row').forEach((row) => refreshDictRowA11y(row, fieldTitle));
    }

    function showDictEditorProblems(editorEl, problems) {
        const errorEl = editorEl.querySelector('.field-error');
        if (!errorEl) return;
        if (problems.length === 0) {
            errorEl.hidden = true;
            errorEl.textContent = '';
        } else {
            errorEl.hidden = false;
            errorEl.textContent = problems.join('\n');
        }
    }

    function initDictEditor(editorEl, onChange) {
        const fieldTitle = dictFieldTitle(editorEl);
        setDictEditorValue(editorEl, JSON.parse(editorEl.dataset.initial || '{}'));

        editorEl.addEventListener('click', (event) => {
            const button = event.target.closest('button');
            if (!button) return;
            if (button.classList.contains('dict-add')) {
                const rowsEl = editorEl.querySelector('.dict-rows');
                rowsEl.insertAdjacentHTML(
                    'beforeend',
                    renderDictRowHtml('', dictValueOptions(editorEl)[0], dictValueOptions(editorEl), fieldTitle),
                );
                const newRow = rowsEl.lastElementChild;
                refreshDictRowA11y(newRow, fieldTitle);
                newRow.querySelector('.dict-key').focus();
                onChange();
            } else if (button.classList.contains('dict-remove')) {
                button.closest('.dict-row').remove();
                onChange();
            }
        });

        editorEl.addEventListener('input', (event) => {
            if (event.target.classList.contains('dict-key')) {
                refreshDictRowA11y(event.target.closest('.dict-row'), fieldTitle);
            }
        });
    }

    function initDictEditors(rootEl, onChange) {
        rootEl.querySelectorAll('[data-type="dict_enum"]').forEach((editorEl) => {
            initDictEditor(editorEl, onChange);
        });
    }

    // --- whole-form collection ---------------------------------------------

    const VALUE_COLLECTORS = {
        boolean: (el) => el.checked,
        enum: (el) => el.value,
        integer: (el) => parseInt(el.value, 10) || 0,
        number: (el) => {
            const parsed = parseFloat(el.value);
            return Number.isNaN(parsed) ? 0 : parsed;
        },
        string: (el) => el.value,
        optional_string: (el) => (el.value === '' ? null : el.value),
        dict_enum: (el) => collectDictEditor(el).value,
    };

    function collectFieldValue(el) {
        const kind = el.dataset.type;
        const collector = VALUE_COLLECTORS[kind];
        if (!collector) {
            throw new Error(`Unsupported settings control kind: ${kind}`);
        }
        return collector(el);
    }

    // Collect the full typed payload plus any field-level problems that
    // must block save (e.g. dict rows with empty/duplicate keys).
    // Inline problem text is surfaced on save attempts
    // (opts.reportProblems) and live-updated afterwards only while an
    // error is already visible - a freshly added, not-yet-filled row
    // should not flash an error before the user has typed anything.
    function collectForm(rootEl, opts = {}) {
        const payload = {};
        const problems = [];
        rootEl.querySelectorAll('[data-tab][data-field]').forEach((el) => {
            const tab = el.dataset.tab;
            const field = el.dataset.field;
            if (!payload[tab]) payload[tab] = {};
            if (el.dataset.type === 'dict_enum') {
                const result = collectDictEditor(el);
                payload[tab][field] = result.value;
                problems.push(...result.problems);
                const errorEl = el.querySelector('.field-error');
                if (opts.reportProblems || (errorEl && !errorEl.hidden)) {
                    showDictEditorProblems(el, result.problems);
                }
            } else {
                payload[tab][field] = collectFieldValue(el);
            }
        });
        return { payload, problems };
    }

    function resetFieldValue(el, value) {
        const kind = el.dataset.type;
        if (!(kind in VALUE_COLLECTORS)) {
            throw new Error(`Unsupported settings control kind: ${kind}`);
        }
        if (kind === 'boolean') {
            el.checked = !!value;
        } else if (kind === 'dict_enum') {
            setDictEditorValue(el, value || {});
            showDictEditorProblems(el, []);
        } else {
            el.value = value !== undefined && value !== null ? value : '';
        }
    }

    return {
        collectDictEntries,
        collectDictEditor,
        collectFieldValue,
        collectForm,
        escapeHtml,
        initDictEditors,
        renderDictRowHtml,
        resetFieldValue,
        setDictEditorValue,
        showDictEditorProblems,
    };
});
