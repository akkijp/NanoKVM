// Run: pnpm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { watchFinished } from './update-watch.ts';

test('a new version that runs after a restart is finished', () => {
  const state = { target: '2.5.4.2', previous: '2.5.4.1', startedFrom: 100, sawPending: false };
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 200 }), true);
});

test('reinstalling the running version is not finished until a restart is seen', () => {
  // Same version, upload answered 502 before reaching the device: the old server still answers.
  const state = { target: '2.5.4.2', previous: '2.5.4.2', startedFrom: 100, sawPending: false };
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 100 }), false);
  // Once the server has restarted (or a pending record was seen), it is.
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 300 }), true);
  assert.equal(
    watchFinished({ ...state, sawPending: true }, { current: '2.5.4.2', serverStarted: 100 }),
    true
  );
});

test('pending or another version is not finished', () => {
  const state = { target: '2.5.4.2', previous: '2.5.4.1', startedFrom: 100, sawPending: true };
  assert.equal(watchFinished(state, { current: '2.5.4.2', restartPending: true, serverStarted: 200 }), false);
  assert.equal(watchFinished(state, { current: '2.5.4.1', serverStarted: 200 }), false);
});

test('an unknown start time is no proof of a restart', () => {
  // The first version query failed: startedFrom is 0. A reinstall of the running version that
  // never reached the device must not count as finished when the old server first answers.
  const state = { target: '2.5.4.2', previous: '2.5.4.2', startedFrom: 0, sawPending: false };
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 100 }), false);
  // A different version is proof by itself.
  assert.equal(
    watchFinished({ ...state, previous: '2.5.4.1' }, { current: '2.5.4.2', serverStarted: 100 }),
    true
  );
});
