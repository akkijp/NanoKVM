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
// really happened:
//   - a different version: the version changed from the one running before;
//   - the running version again (a reinstall): the server start time changed, with both times
//     known. A pending record seen on the way is not enough: a failed install that put the old
//     version back also clears it, without any restart.
export function watchFinished(state: WatchState, reply: VersionReply): boolean {
  if (reply.restartPending) return false;
  if (state.target && reply.current !== state.target) return false;
  const serverRestarted =
    state.startedFrom > 0 && !!reply.serverStarted && reply.serverStarted !== state.startedFrom;
  const sameVersion = !state.previous || !state.target || state.target === state.previous;
  if (sameVersion) return serverRestarted;
  return reply.current !== state.previous;
}
