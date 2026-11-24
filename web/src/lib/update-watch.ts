// akkijp fork: when a page watching its own update (or restart retry) may consider it finished.
// Kept pure so it can be tested without a browser (update-watch.test.ts).

export type WatchState = {
  target: string; // the version the update should bring up ('' if unknown)
  previous: string; // the version running when the watch began ('' if unknown)
  startedFrom: number; // serverStarted when the watch began (0 if unknown)
  sawPending: boolean; // a pending record was seen during the watch
  // The device's install count when the watch began; null when nothing is installed (a restart
  // retry), -1 if unknown (an install that then cannot be confirmed for the running version).
  installsFrom: number | null;
};

export type VersionReply = {
  current: string;
  restartPending?: boolean;
  serverStarted?: number;
  installs?: number;
};

// Finished when the target version runs, nothing is pending, and there is proof that the update
// really happened:
//   - a different version: the version changed from the one running before;
//   - the running version again (a reinstall): the server start time changed, with both times
//     known, and the device counted one more install (a restart alone, e.g. after a power cut
//     during the upload, does not prove the install). A restart retry needs only the restart.
//     A pending record seen on the way is not enough: a failed install that put the old version
//     back also clears it, without any restart.
export function watchFinished(state: WatchState, reply: VersionReply): boolean {
  if (reply.restartPending) return false;
  if (state.target && reply.current !== state.target) return false;
  const serverRestarted =
    state.startedFrom > 0 && !!reply.serverStarted && reply.serverStarted !== state.startedFrom;
  const sameVersion = !state.previous || !state.target || state.target === state.previous;
  if (sameVersion) {
    if (!serverRestarted) return false;
    if (state.installsFrom === null) return true; // restart retry
    return (
      state.installsFrom >= 0 && typeof reply.installs === 'number' && reply.installs > state.installsFrom
    );
  }
  return reply.current !== state.previous;
}

// The watch survives the page: a restart changes the server's signing key, so the next query is
// answered 401 and the user logs in again. Kept in sessionStorage (this tab only).
export type SavedWatch = {
  target: string;
  previous: string;
  startedFrom: number;
  installsFrom: number | null;
  until: number;
};

const WATCH_KEY = 'nanokvm-update-watch';

export function saveWatch(watch: SavedWatch, storage: Pick<Storage, 'setItem'> = sessionStorage) {
  try {
    storage.setItem(WATCH_KEY, JSON.stringify(watch));
  } catch {
    // storage unavailable: the watch then ends with the page
  }
}

export function loadWatch(storage: Pick<Storage, 'getItem'> = sessionStorage): SavedWatch | null {
  try {
    const raw = storage.getItem(WATCH_KEY);
    if (!raw) return null;
    const w = JSON.parse(raw);
    if (
      typeof w?.target === 'string' &&
      typeof w?.previous === 'string' &&
      typeof w?.startedFrom === 'number' &&
      typeof w?.until === 'number'
    ) {
      // Saved without a count (unknown): an install of the running version cannot be confirmed.
      const installsFrom = w.installsFrom === null || typeof w.installsFrom === 'number' ? w.installsFrom : -1;
      return { ...w, installsFrom };
    }
  } catch {
    // unreadable: treat as none
  }
  return null;
}

export function clearWatch(storage: Pick<Storage, 'removeItem'> = sessionStorage) {
  try {
    storage.removeItem(WATCH_KEY);
  } catch {
    // ignore
  }
}
