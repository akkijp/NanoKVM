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

test('numeric parts are compared as numbers, up to 9 digits', () => {
  assert.equal(compareVersions('2.5.4.999999999', '2.5.4.999999998'), 1);
  assert.equal(compareVersions('2.5.4.0010', '2.5.4.10'), 0);
});

test('a part longer than 9 digits is unknown, as on the server', () => {
  assert.equal(compareVersions('2.5.4.1700000000', '2.5.4.2'), null);
  assert.equal(versionStatus('2.5.4.1700000000', '2.5.4.2'), 'unknown');
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

test('package names follow the 1-9 digit rule', () => {
  assert.equal(isValidPackageName('nanokvm_999999999.999999999.999999999.999999999.tar.gz'), true);
  assert.equal(isValidPackageName('nanokvm_1234567890.0.0.1.tar.gz'), false);
});
