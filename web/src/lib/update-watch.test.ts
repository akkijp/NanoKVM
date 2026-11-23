// Run: pnpm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { clearWatch, loadWatch, saveWatch, watchFinished } from './update-watch.ts';

test('a new version that runs after a restart is finished', () => {
  const state = { target: '2.5.4.2', previous: '2.5.4.1', startedFrom: 100, sawPending: false };
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 200 }), true);
});

test('reinstalling the running version is not finished until a restart is seen', () => {
  // Same version, upload answered 502 before reaching the device: the old server still answers.
  const state = { target: '2.5.4.2', previous: '2.5.4.2', startedFrom: 100, sawPending: false };
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 100 }), false);
  // Once the server has restarted, it is.
  assert.equal(watchFinished(state, { current: '2.5.4.2', serverStarted: 300 }), true);
  // A record seen on the way is not proof: a failed install that put the old version back clears
  // it too, and the old server never restarted.
  assert.equal(
    watchFinished({ ...state, sawPending: true }, { current: '2.5.4.2', serverStarted: 100 }),
    false
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

test('a watch survives a re-login (stored per tab)', () => {
  const store = new Map<string, string>();
  const storage = {
    setItem: (k: string, v: string) => void store.set(k, v),
    getItem: (k: string) => store.get(k) ?? null,
    removeItem: (k: string) => void store.delete(k)
  };
  const watch = { target: '2.5.4.2', previous: '2.5.4.1', startedFrom: 100, until: 9e12 };
  saveWatch(watch, storage);
  assert.deepEqual(loadWatch(storage), watch);
  clearWatch(storage);
  assert.equal(loadWatch(storage), null);
  store.set('nanokvm-update-watch', '{"target": 1}');
  assert.equal(loadWatch(storage), null);
});
