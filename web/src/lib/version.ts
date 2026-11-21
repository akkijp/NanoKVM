// akkijp fork: versions are X.Y.Z or X.Y.Z.N (e.g. 2.5.4.1). semver cannot parse the
// four-part form, so compare the numeric parts here. A missing fourth part counts as 0.
// Pre-release / build suffixes are ignored. Unparseable input returns null.
const VERSION_RE = /^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$/;

function parse(version: string): number[] | null {
  const m = VERSION_RE.exec(version.trim());
  if (!m) return null;
  return [m[1], m[2], m[3], m[4] ?? '0'].map((p) => Number(p));
}

export function compareVersions(a: string, b: string): number | null {
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return null;
  for (let i = 0; i < 4; i++) {
    if (pa[i] !== pb[i]) return pa[i] > pb[i] ? 1 : -1;
  }
  return 0;
}

export function isNewerVersion(latest: string, current: string): boolean {
  return compareVersions(latest, current) === 1;
}

export function isUpToDate(current: string, latest: string): boolean {
  const c = compareVersions(current, latest);
  return c === null ? true : c >= 0;
}
