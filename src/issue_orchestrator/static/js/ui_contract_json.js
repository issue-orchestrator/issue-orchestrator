// Shared fail-closed JSON ingestion for browser UI payloads (issue #6337).
//
// Every JSON boundary into dashboard JavaScript — DOM ``data-*``
// attributes, inline ``<script type="application/json">`` bootstraps,
// ``fetch().json()`` responses, and event/stream payloads — goes through
// one of the readers here.  Each reader parses, validates against the
// generated ``ui-contracts.validators.js`` schema, and returns the
// payload only if it satisfies the contract.  Malformed payloads return
// ``null`` after a single diagnostic, so feature code fails closed by
// checking for ``null`` instead of hand-rolling shape checks.
//
// Ownership boundary:
//   - This module + the generated validators own JSON *payload shape*:
//     required fields, kinds, enums, forbidden extras, numeric bounds.
//   - UI owner abstractions own *contextual* invariants a schema cannot
//     know, e.g. ``resolveRowCommandContext()`` checking that a valid
//     command's ``run_id`` matches the DOM row that dispatched it.
//
// Feature code should not call ``uiContractValidators.validate``
// directly; going through a reader is what keeps the diagnostics path
// (and the fail-closed contract) in one place.
(function (root, factory) {
    const validators = typeof module === 'object' && module.exports
        ? require('./ui-contracts.validators.js')
        : root.uiContractValidators;
    const api = factory(validators);
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.uiContractJson = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (validators) {
    if (!validators) {
        throw new Error('uiContractValidators must load before ui_contract_json.js');
    }

    let _reporter = null;

    // One observable failure path for every rejected payload: a console
    // error always, plus a toast when the dashboard shell provides one.
    // Silent no-ops are the failure mode this layer exists to remove —
    // a malformed payload should be diagnosable from the console alone.
    function _defaultReporter(violation) {
        const message = describeViolation(violation);
        if (typeof console !== 'undefined' && console.error) {
            console.error('[ui-contract] ' + message, violation);
        }
        const globalScope = typeof globalThis !== 'undefined' ? globalThis : null;
        const toast = globalScope && globalScope.showToast;
        if (typeof toast === 'function') {
            toast(message, 'error');
        }
    }

    // Lets the host (or a test) observe rejections. Pass null to restore
    // the console/toast default.
    function setViolationReporter(reporter) {
        _reporter = typeof reporter === 'function' ? reporter : null;
    }

    function describeViolation(violation) {
        const where = violation.source ? ' from ' + violation.source : '';
        const detail = violation.errors && violation.errors.length
            ? violation.errors.join('; ')
            : violation.detail;
        return 'Rejected ' + violation.schemaName + ' payload' + where + ': ' + detail;
    }

    function _reject(violation) {
        (_reporter || _defaultReporter)(violation);
        return null;
    }

    // Validates an already-decoded value (e.g. an inline bootstrap object
    // the template wrote onto ``window``). Returns the payload, or null.
    function fromValue(value, schemaName, source) {
        const result = validators.validate(schemaName, value);
        if (result.ok) return result.value;
        return _reject({
            schemaName: schemaName,
            source: source || 'value',
            detail: 'payload does not satisfy the contract',
            errors: result.errors,
        });
    }

    // Reads one message off a channel several message families share (a
    // window's postMessage stream). ``undefined``: the value names none of
    // the discriminated union's members, so it is not this reader's message
    // and nothing is reported. Otherwise the payload validated against the
    // union, or ``null`` after one diagnostic. The member tags come from the
    // generated contract, so callers never hand-write them.
    function fromUnionMember(value, unionSchemaName, source) {
        const union = validators.SCHEMAS[unionSchemaName];
        if (!union || !union.discriminator || !Array.isArray(union.oneOf)) {
            return _reject({
                schemaName: unionSchemaName,
                source: source || 'value',
                detail: 'schema is not a discriminated union',
                errors: [],
            });
        }
        const tag = union.discriminator.propertyName;
        const tags = union.oneOf.map(member => {
            const name = String(member.$ref || '').split('/').pop();
            const property = ((validators.SCHEMAS[name] || {}).properties || {})[tag] || {};
            return property.const;
        });
        const candidate = value !== null && typeof value === 'object' ? value[tag] : undefined;
        if (!tags.includes(candidate)) return undefined;
        return fromValue(value, unionSchemaName, source);
    }

    // Parses raw JSON text and validates it. Returns the payload, or null.
    function parse(rawText, schemaName, source) {
        const label = source || 'json text';
        if (typeof rawText !== 'string' || rawText.trim() === '') {
            return _reject({
                schemaName: schemaName,
                source: label,
                detail: 'payload is empty',
                errors: [],
            });
        }
        let decoded;
        try {
            decoded = JSON.parse(rawText);
        } catch (error) {
            return _reject({
                schemaName: schemaName,
                source: label,
                detail: 'payload is not valid JSON (' + _errorText(error) + ')',
                errors: [],
            });
        }
        return fromValue(decoded, schemaName, label);
    }

    // Reads a DOM ``data-*`` JSON payload. ``datasetKey`` is the camelCase
    // dataset name, e.g. 'lifecycleCommand' for ``data-lifecycle-command``.
    function fromDataset(element, datasetKey, schemaName) {
        const source = 'data-' + _dashCase(datasetKey);
        if (!element || !element.dataset) {
            return _reject({
                schemaName: schemaName,
                source: source,
                detail: 'element has no dataset',
                errors: [],
            });
        }
        return parse(element.dataset[datasetKey], schemaName, source);
    }

    // Reads an inline ``<script type="application/json">`` bootstrap.
    // Accepts the element or its id.
    function fromInlineScript(nodeOrId, schemaName) {
        const node = typeof nodeOrId === 'string' ? _elementById(nodeOrId) : nodeOrId;
        const source = 'inline script#' + (typeof nodeOrId === 'string' ? nodeOrId : (node && node.id) || '?');
        if (!node) {
            return _reject({
                schemaName: schemaName,
                source: source,
                detail: 'inline JSON element is missing',
                errors: [],
            });
        }
        return parse(node.textContent, schemaName, source);
    }

    // Reads a ``fetch`` response body. HTTP status handling stays with the
    // caller: this validates the decoded body only.
    async function fromResponse(response, schemaName, source) {
        const label = source || (response && response.url) || 'fetch response';
        if (!response || typeof response.text !== 'function') {
            return _reject({
                schemaName: schemaName,
                source: label,
                detail: 'not a fetch response',
                errors: [],
            });
        }
        let rawText;
        try {
            rawText = await response.text();
        } catch (error) {
            return _reject({
                schemaName: schemaName,
                source: label,
                detail: 'response body could not be read (' + _errorText(error) + ')',
                errors: [],
            });
        }
        return parse(rawText, schemaName, label);
    }

    // Reads a streaming payload — the ``data`` of an EventSource message
    // or WebSocket frame.
    function fromEventData(rawData, schemaName, source) {
        return parse(rawData, schemaName, source || 'event data');
    }

    // Reads a FAILURE body to build a display string.
    //
    // Error bodies are deliberately outside the contract: the OpenAPI
    // spec defines a schema for the 200 of each UI endpoint, not for its
    // failures, so there is nothing to validate one against. This is the
    // single sanctioned raw read at a fetch boundary — it exists so that
    // "the error body has no schema" is expressed once here rather than
    // as a hand-rolled ``.json().catch(...)`` in every caller, and so a
    // stray ``JSON.parse`` in feature code stays a red flag.
    //
    // Returns a string, never a payload: nothing here can reach a
    // renderer. Callers pass their own fallback for a body that is
    // absent, unreadable, or not JSON.
    //
    // A Control Center failure pairs a machine ``error`` code with a human
    // ``detail`` (``passthrough_failed`` + why the engine did not answer,
    // #8222), so a non-empty string ``detail`` wins when both are present:
    // the code alone tells the operator nothing they can act on.
    async function errorMessage(response, fallback) {
        const status = response && response.status;
        const defaultText = fallback || 'HTTP ' + (status || 'error');
        if (!response || typeof response.text !== 'function') return defaultText;
        try {
            const body = JSON.parse(await response.text());
            const hasDetailText = body && typeof body === 'object'
                && typeof body.detail === 'string' && body.detail.trim() !== '';
            const message = body && typeof body === 'object'
                ? (hasDetailText ? body.detail : body.error || body.detail)
                : null;
            return String(message || defaultText);
        } catch (_) {
            return defaultText;
        }
    }

    function _elementById(id) {
        const globalScope = typeof globalThis !== 'undefined' ? globalThis : null;
        const doc = globalScope && globalScope.document;
        return doc && typeof doc.getElementById === 'function' ? doc.getElementById(id) : null;
    }

    function _dashCase(key) {
        return String(key).replace(/[A-Z]/g, (letter) => '-' + letter.toLowerCase());
    }

    function _errorText(error) {
        return error instanceof Error ? error.message : String(error);
    }

    return {
        describeViolation,
        errorMessage,
        fromDataset,
        fromEventData,
        fromInlineScript,
        fromResponse,
        fromUnionMember,
        fromValue,
        parse,
        setViolationReporter,
    };
});
