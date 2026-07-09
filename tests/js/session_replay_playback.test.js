// Behavior tests for the session-replay playback timing + state policy
// (issue #6583). The two functions under test are pure — they carry the
// "why does playback look stuck / where am I" logic without touching the DOM,
// so we can exercise them directly in a node:vm context.
//
//   - computeSessionReplayStepDelay: compresses long idle gaps so progress
//     stays visible and early output is reachable within ~1s of Play.
//   - describeSessionReplayPlayback: names the playback state (empty / start /
//     playing / paused / end) so the viewer never looks ambiguously frozen.

const test = require('node:test');
// Non-strict assert: vm.runInContext objects have cross-realm prototypes.
const assert = require('node:assert');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function loadSessionReplay() {
    // session_replay.js only declares variables + functions at module scope;
    // every DOM/Terminal reference is inside a function body, so a bare context
    // is enough to load it and pull out the pure helpers.
    const context = { console };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(
                __dirname,
                '../../src/issue_orchestrator/static/js/dashboard/session_replay.js',
            ),
            'utf8',
        ),
        context,
        { filename: 'session_replay.js' },
    );
    return context;
}

const { computeSessionReplayStepDelay, describeSessionReplayPlayback } = loadSessionReplay();

// ---------------------------------------------------------------------------
// computeSessionReplayStepDelay
// ---------------------------------------------------------------------------

test('short output-burst gaps keep their natural cadence', () => {
    const events = [{ offset_ms: 0 }, { offset_ms: 40 }, { offset_ms: 90 }];
    assert.equal(computeSessionReplayStepDelay(events, 1, 1), 40);
    assert.equal(computeSessionReplayStepDelay(events, 2, 1), 50);
});

test('long idle gaps are compressed to the ceiling so the scrubber advances', () => {
    // A five-minute human-typing / idle pause must not freeze playback.
    const events = [{ offset_ms: 0 }, { offset_ms: 300000 }];
    assert.equal(computeSessionReplayStepDelay(events, 1, 1), 1000);
});

test('the very first event is reachable quickly even with a long lead-in offset', () => {
    // Early output within the first second of a chapter is the whole point of
    // #6583: the first step is measured from zero and capped.
    const events = [{ offset_ms: 8000 }, { offset_ms: 8050 }];
    assert.equal(computeSessionReplayStepDelay(events, 0, 1), 1000);
});

test('speed multiplier shortens the (already capped) delay', () => {
    const events = [{ offset_ms: 0 }, { offset_ms: 300000 }];
    assert.equal(computeSessionReplayStepDelay(events, 1, 4), 250);
    assert.equal(computeSessionReplayStepDelay(events, 1, 2), 500);
    // Sub-1x slows real bursts back down.
    const burst = [{ offset_ms: 0 }, { offset_ms: 40 }];
    assert.equal(computeSessionReplayStepDelay(burst, 1, 0.5), 80);
});

test('out-of-range and malformed inputs yield an immediate (0ms) step', () => {
    const events = [{ offset_ms: 0 }, { offset_ms: 40 }];
    assert.equal(computeSessionReplayStepDelay(events, 5, 1), 0);
    assert.equal(computeSessionReplayStepDelay(events, -1, 1), 0);
    assert.equal(computeSessionReplayStepDelay(null, 0, 1), 0);
    // Non-monotonic offsets clamp to 0 rather than scheduling a negative delay.
    const backwards = [{ offset_ms: 500 }, { offset_ms: 100 }];
    assert.equal(computeSessionReplayStepDelay(backwards, 1, 1), 0);
    // Missing offsets are treated as 0.
    const missing = [{}, {}];
    assert.equal(computeSessionReplayStepDelay(missing, 1, 1), 0);
});

// ---------------------------------------------------------------------------
// describeSessionReplayPlayback
// ---------------------------------------------------------------------------

test('zero events is reported as an explicit empty state, not a paused one', () => {
    const state = describeSessionReplayPlayback({ total: 0, current: 0, playing: false });
    assert.equal(state.key, 'empty');
    assert.match(state.label, /no events/i);
});

test('start is distinct from paused-in-the-middle', () => {
    const atStart = describeSessionReplayPlayback({ total: 100, current: 0, playing: false });
    assert.equal(atStart.key, 'start');
    assert.match(atStart.label, /start/i);

    const midway = describeSessionReplayPlayback({ total: 100, current: 42, playing: false });
    assert.equal(midway.key, 'paused');
    assert.match(midway.label, /42 \/ 100/);
});

test('playing surfaces speed and live progress', () => {
    const state = describeSessionReplayPlayback({ total: 100, current: 7, playing: true, speed: 2 });
    assert.equal(state.key, 'playing');
    assert.match(state.label, /2x/);
    assert.match(state.label, /7 \/ 100/);
});

test('end distinguishes following-live from paused-at-end', () => {
    const live = describeSessionReplayPlayback({ total: 100, current: 100, playing: false, follow: true });
    assert.equal(live.key, 'end');
    assert.match(live.label, /latest/i);

    const parked = describeSessionReplayPlayback({ total: 100, current: 100, playing: false, follow: false });
    assert.equal(parked.key, 'end');
    assert.match(parked.label, /end/i);
});
