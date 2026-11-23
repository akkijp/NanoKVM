// akkijp fork: when a page watching its own update (or restart retry) may consider it finished.
// Kept pure so it can be tested without a browser (update-watch.test.ts).

export type WatchState = {
  target: string; // the version the update should bring up ('' if unknown)
  startedFrom: number; // serverStarted when the watch began
  sawPending: boolean; // a pending record was seen during the watch
};

export type VersionReply = {
  current: string;
  restartPending?: boolean;
  serverStarted?: number;
};

// Finished when the target version runs, nothing is pending, and there is proof that the update
// really did something: the server restarted, or a pending record was seen on the way. Without
// that proof a reinstall of the running version would look finished before it even started.
export function watchFinished(state: WatchState, reply: VersionReply): boolean {
  if (reply.restartPending) return false;
  const restarted =
    state.sawPending || (!!reply.serverStarted && reply.serverStarted !== state.startedFrom);
  if (!restarted) return false;
  return !state.target || reply.current === state.target;
}
