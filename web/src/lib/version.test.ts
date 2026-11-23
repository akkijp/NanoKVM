// Run: pnpm test   (node --experimental-strip-types --test)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { compareVersions, isNewerVersion, isValidPackageName, versionStatus } from './version.ts';

test('four-part versions', () => {
  assert.equal(compareVersions('2.5.4.1', '2.5.4'), 1);
  assert.equal(compareVersions('2.5.4', '2.5.4.0'), 0);
  assert.equal(compareVersions('2.5.4.2', '2.5.4.10'), -1);
  assert.equal(compareVersions('2.5.5', '2.5.4.9'), 1);
  assert.ok(isNewerVersion('2.5.4.1', '2.4.3'));
  assert.ok(!isNewerVersion('2.5.4', '2.5.4.1'));
});

test('pre-release ordering follows semver', () => {
  assert.equal(compareVersions('2.5.4-rc.1', '2.5.4-rc.2'), -1);
  assert.equal(compareVersions('2.5.4-rc.2', '2.5.4'), -1);
  assert.equal(compareVersions('2.5.4-rc.10', '2.5.4-rc.2'), 1);
  assert.equal(compareVersions('2.5.4-alpha', '2.5.4-alpha.1'), -1);
  assert.equal(compareVersions('2.5.4-1', '2.5.4-alpha'), -1);
  assert.equal(compareVersions('2.5.4.1-rc.1', '2.5.4.1'), -1);
  assert.equal(compareVersions('2.5.4+build.1', '2.5.4'), 0);
  assert.equal(versionStatus('2.5.4-rc.1', '2.5.4'), 'outdated');
});

test('large numeric parts are not rounded', () => {
  assert.equal(compareVersions('2.5.4.9007199254740993', '2.5.4.9007199254740992'), 1);
  assert.equal(compareVersions('2.5.4.0010', '2.5.4.10'), 0);
});

test('unparseable versions are unknown, not up to date', () => {
  for (const bad of ['', ' ', '2.5', '2.5.4.1.2', '2.5.4.x', 'v2.5.4']) {
    assert.equal(compareVersions(bad, '2.5.4'), null, bad);
    assert.equal(versionStatus(bad, '2.5.4'), 'unknown', bad);
    assert.ok(!isNewerVersion('2.5.4', bad), bad);
  }
  assert.equal(versionStatus('2.5.4.1', '2.5.4.1'), 'latest');
  assert.equal(versionStatus('2.5.4', '2.5.4.1'), 'outdated');
});

test('offline package names', () => {
  assert.ok(isValidPackageName('nanokvm_2.5.4.tar.gz'));
  assert.ok(isValidPackageName('nanokvm_2.5.4.1.tar.gz'));
  for (const bad of ['nanokvm_2.5.4.1.2.tar.gz', 'nanokvm_2.5.tar.gz', 'nanokvm_2.5.4.x.tar.gz', 'nanokvm_2.5.4.1.zip']) {
    assert.ok(!isValidPackageName(bad), bad);
  }
});
