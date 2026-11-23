// akkijp fork: when a page watching its own update (or restart retry) may consider it finished.
// Kept pure so it can be tested without a browser (update-watch.test.ts).

export type WatchState = {
  target: string; // the version the update should bring up ('' if unknown)
  previous: string; // the version running when the watch began ('' if unknown)
  startedFrom: number; // serverStarted when the watch began (0 if unknown)
  sawPending: boolean; // a pending record was seen during the watch
};

export type VersionReply = {
  current: string;
  restartPending?: boolean;
  serverStarted?: number;
};

// Finished when the target version runs, nothing is pending, and there is proof that the update
// really did something: the version changed, a pending record was seen on the way, or the server
// start time changed (only when both times are known). Without that proof a reinstall of the
// running version would look finished before it even started.
export function watchFinished(state: WatchState, reply: VersionReply): boolean {
  if (reply.restartPending) return false;
  if (state.target && reply.current !== state.target) return false;
  const versionChanged = !!state.previous && reply.current !== state.previous;
  const serverRestarted =
    state.startedFrom > 0 && !!reply.serverStarted && reply.serverStarted !== state.startedFrom;
  return versionChanged || state.sawPending || serverRestarted;
}
