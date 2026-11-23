// akkijp fork: versions are X.Y.Z or X.Y.Z.N (e.g. 2.5.4.1), optionally followed by a
// semver pre-release (-rc.1) and build metadata (+meta). semver cannot parse the four-part
// form, so compare here. A missing fourth part counts as 0. Each numeric part is 1-9 digits
// (the rule of the server, the updater and scripts/package.sh); numbers are compared as digit
// strings. Pre-release ordering follows semver 2.0.0.
// Unparseable input yields null so callers can tell "unknown" apart from "up to date".
const VERSION_RE =
  /^(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?:\.(\d{1,9}))?(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;

type Parsed = { core: string[]; pre: string[] };

function normalizeDigits(d: string): string {
  const s = d.replace(/^0+/, '');
  return s === '' ? '0' : s;
}

function compareDigits(a: string, b: string): number {
  const x = normalizeDigits(a);
  const y = normalizeDigits(b);
  if (x.length !== y.length) return x.length > y.length ? 1 : -1;
  if (x === y) return 0;
  return x > y ? 1 : -1;
}

function parse(version: string): Parsed | null {
  if (typeof version !== 'string') return null;
  const m = VERSION_RE.exec(version.trim());
  if (!m) return null;
  return { core: [m[1], m[2], m[3], m[4] ?? '0'], pre: m[5] ? m[5].split('.') : [] };
}

function comparePre(a: string[], b: string[]): number {
  if (a.length === 0 && b.length === 0) return 0;
  if (a.length === 0) return 1; // a release is newer than its pre-releases
  if (b.length === 0) return -1;
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const an = /^\d+$/.test(a[i]);
    const bn = /^\d+$/.test(b[i]);
    let c: number;
    if (an && bn) c = compareDigits(a[i], b[i]);
    else if (an) c = -1;
    else if (bn) c = 1;
    else c = a[i] === b[i] ? 0 : a[i] > b[i] ? 1 : -1;
    if (c !== 0) return c;
  }
  return a.length === b.length ? 0 : a.length > b.length ? 1 : -1;
}

export function compareVersions(a: string, b: string): number | null {
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return null;
  for (let i = 0; i < 4; i++) {
    const c = compareDigits(pa.core[i], pb.core[i]);
    if (c !== 0) return c;
  }
  return comparePre(pa.pre, pb.pre);
}

export function isNewerVersion(latest: string, current: string): boolean {
  return compareVersions(latest, current) === 1;
}

export type VersionStatus = 'latest' | 'outdated' | 'unknown';

export function versionStatus(current: string, latest: string): VersionStatus {
  const c = compareVersions(current, latest);
  if (c === null) return 'unknown';
  return c >= 0 ? 'latest' : 'outdated';
}

// Offline update packages: nanokvm_X.Y.Z.tar.gz or nanokvm_X.Y.Z.N.tar.gz.
// Must match packageNamePattern in server/service/application/version.go.
// Every part 1-9 digits: the same rule as the server and scripts/package.sh.
export const PACKAGE_NAME_RE = /^nanokvm_\d{1,9}\.\d{1,9}\.\d{1,9}(?:\.\d{1,9})?\.tar\.gz$/;

export function isValidPackageName(filename: string): boolean {
  return PACKAGE_NAME_RE.test(filename);
}
