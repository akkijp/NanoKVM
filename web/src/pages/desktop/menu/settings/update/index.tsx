import { useEffect, useRef, useState } from 'react';
import { LoadingOutlined, RocketOutlined, SmileOutlined } from '@ant-design/icons';
import { Alert, Button, Divider, Result, Spin } from 'antd';
import { useTranslation } from 'react-i18next';

import * as api from '@/api/application.ts';

import { Offline } from './offline.tsx';
import { versionStatus } from '@/lib/version.ts';
import { clearWatch, loadWatch, saveWatch, watchFinished } from '@/lib/update-watch.ts';

type UpdateProps = {
  setIsLocked: (isClosable: boolean) => void;
};

export const Update = ({ setIsLocked }: UpdateProps) => {
  const { t } = useTranslation();

  const [status, setStatus] = useState('');
  const [currentVersion, setCurrentVersion] = useState('');
  const [latestVersion, setLatestVersion] = useState('');
  const [errMsg, setErrMsg] = useState('');
  const [microphone, setMicrophone] = useState('');
  const [picoclaw, setPicoclaw] = useState('');
  const versionRequestRef = useRef(0);
  const recheckTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // While watching an update or a restart, a failed request (the server restarting) is retried
  // until this time instead of ending in "failed".
  const watchUntilRef = useRef(0);
  const reloadWhenSettledRef = useRef(false);
  // The version the watched update or restart should bring up; reload only once it runs.
  const watchTargetRef = useRef('');
  // Proof that the watched update did something: the server restarted, or a pending record was
  // seen. Without it a reinstall of the running version could look finished before it started.
  const watchStartedFromRef = useRef(0);
  const watchSawPendingRef = useRef(false);
  const watchPreviousRef = useRef('');
  // The version and server start time taken right before the current request (ensureBaseline).
  const baselineRef = useRef({ version: '', started: 0 });
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    // A watch started before the page went away (e.g. the restart signed us out and we logged in
    // again): resume it, so the result is still confirmed and the new page loaded.
    const saved = loadWatch();
    if (saved && Date.now() < saved.until) {
      baselineRef.current = { version: saved.previous, started: saved.startedFrom };
      watchTargetRef.current = saved.target;
      watchStartedFromRef.current = saved.startedFrom;
      watchPreviousRef.current = saved.previous;
      watchSawPendingRef.current = false;
      reloadWhenSettledRef.current = true;
      watchUntilRef.current = saved.until;
      setIsLocked(true);
      setStatus('updating');
    } else if (saved) {
      clearWatch();
    }
    checkForUpdates();
    return () => {
      // akkijp fork: no re-check after the page is gone (and none from a reply still in flight).
      mountedRef.current = false;
      if (recheckTimerRef.current) clearTimeout(recheckTimerRef.current);
    };
  }, []);

  function checkForUpdates() {
    const requestId = ++versionRequestRef.current;
    setStatus('loading');

    api
      .getVersion()
      .then((rsp: any) => {
        if (requestId !== versionRequestRef.current || !mountedRef.current) return;
        if (rsp.code !== 0 || !rsp.data) {
          watchUntilRef.current = 0;
          reloadWhenSettledRef.current = false;
          setIsLocked(false);
          setStatus('failed');
          setErrMsg(t('settings.update.queryFailed'));
          return;
        }

        setCurrentVersion(rsp.data.current);

        // akkijp fork: an update has not been seen running yet. If it is complete, offer to retry
        // the restart; if it stopped half-way, only manual recovery is safe.
        setMicrophone(rsp.data.microphone || '');
        setPicoclaw(rsp.data.picoclaw || '');

        // Watching our own update or restart retry.
        if (reloadWhenSettledRef.current) {
          const pending = rsp.data.restartPending;
          if (pending) watchSawPendingRef.current = true;
          const state = {
            target: watchTargetRef.current,
            previous: watchPreviousRef.current,
            startedFrom: watchStartedFromRef.current,
            sawPending: watchSawPendingRef.current
          };
          if (watchFinished(state, rsp.data)) {
            const bootFailed =
              (rsp.data.microphone || '').startsWith('FAILED') || (rsp.data.picoclaw || '').startsWith('FAILED');
            clearWatch();
            if (!bootFailed) {
              window.location.reload(); // the target version runs, after a real restart, and has settled
              return;
            }
            // Updated, but the microphone stayed on: stop watching and keep the page (the warning
            // is shown above); the version and update button below come from the normal checks.
            watchUntilRef.current = 0;
            reloadWhenSettledRef.current = false;
            setIsLocked(false);
            setErrMsg('');
          }
          if (!reloadWhenSettledRef.current) {
            // finished above (microphone warning): fall through to the normal display
          } else if (pending && (rsp.data.restartPhase === 'switching' || rsp.data.restartPhase === 'installed')) {
            // Stopped half-way (manual recovery) or the restart failed (retry): stop watching and
            // fall through to show it, with the settings unlocked.
            reloadWhenSettledRef.current = false;
          } else if (Date.now() >= watchUntilRef.current) {
            stopWatching(t('settings.update.unconfirmed'));
            return;
          } else {
            setStatus('updating');
            scheduleRecheck();
            return;
          }
        }
        if (rsp.data.restartPending) {
          setLatestVersion(rsp.data.pendingVersion || '');
          if (['confirming', 'updating', 'restarting'].includes(rsp.data.restartPhase)) {
            // Another update, or the restart check, is in progress. Look again shortly, but not
            // forever: past the deadline the settings can be closed (the device carries on).
            if (!watchUntilRef.current) watchUntilRef.current = Date.now() + 5 * 60 * 1000;
            if (Date.now() >= watchUntilRef.current) {
              stopWatching(t('settings.update.unconfirmed'));
              return;
            }
            setStatus(rsp.data.restartPhase === 'confirming' ? 'confirming' : 'updating');
            scheduleRecheck();
          } else {
            // Waiting for the user (retry or manual recovery): stop watching, unlock the settings,
            // and forget the stored watch (a reload must not reopen the update tab for it).
            clearWatch();
            watchUntilRef.current = 0;
            reloadWhenSettledRef.current = false;
            setIsLocked(false);
            setStatus(rsp.data.restartPhase === 'installed' ? 'restartPending' : 'switchIncomplete');
          }
          return;
        }

        if (rsp.data.latestError) {
          // The local state above (microphone) is still shown.
          setStatus('failed');
          setErrMsg(t('settings.update.queryFailed'));
          return;
        }

        if (rsp.data?.latest) {
          setLatestVersion(rsp.data.latest);
          const versionState = versionStatus(rsp.data.current, rsp.data.latest);
          if (versionState === 'unknown') {
            // akkijp fork: an unreadable version must not be reported as up to date.
            setStatus('failed');
            setErrMsg(
              `${t('settings.update.queryFailed')} (current: "${rsp.data.current}", latest: "${rsp.data.latest}")`
            );
          } else {
            setStatus(versionState);
          }
        } else {
          setStatus('latest');
        }
      })
      .catch(() => {
        if (requestId !== versionRequestRef.current || !mountedRef.current) return;
        if (Date.now() < watchUntilRef.current) {
          scheduleRecheck(); // probably the restart: keep the current view and ask again
          return;
        }
        // Gave up watching: let the user close the settings or try again.
        stopWatching(t('settings.update.queryFailed'));
      });
  }

  // An install failed. It may have left a record (switching: recovery; installed: retry the
  // restart); show that instead of a bare error, which would offer an upload the server refuses.
  function showFailure(message: string) {
    clearWatch(); // refused: nothing to resume
    setIsLocked(false);
    api
      .getVersion()
      .then((rsp: any) => {
        if (!mountedRef.current) return;
        const data = rsp?.data;
        if (rsp?.code === 0 && data?.restartPending) {
          // Another update is running, restarting or being confirmed, or one stopped: let the
          // normal query show it (it keeps watching the first three, and asks for recovery only
          // when it stopped half-way).
          checkForUpdates();
          return;
        }
        setStatus('failed');
        setErrMsg(message);
      })
      .catch(() => {
        if (!mountedRef.current) return;
        setStatus('failed');
        setErrMsg(message);
      });
  }

  function stopWatching(message: string) {
    clearWatch();
    watchUntilRef.current = 0;
    reloadWhenSettledRef.current = false;
    watchTargetRef.current = '';
    setIsLocked(false);
    setStatus('failed');
    setErrMsg(message);
  }

  function scheduleRecheck() {
    if (!mountedRef.current) return;
    if (recheckTimerRef.current) clearTimeout(recheckTimerRef.current);
    recheckTimerRef.current = setTimeout(checkForUpdates, 5000);
  }

  async function retryRestart() {
    if (status !== 'restartPending') return;

    setIsLocked(true);
    setStatus('updating');
    if (!(await ensureBaseline()) || !mountedRef.current) {
      setIsLocked(false);
      if (mountedRef.current) {
        setStatus('restartPending');
        setErrMsg(t('settings.update.queryFailed'));
      }
      return;
    }

    const refused = (message: string) => {
      clearWatch();
      // Not restarting: unlock, say why, and keep the retry available.
      setIsLocked(false);
      setStatus('restartPending');
      setErrMsg(message);
    };

    rememberWatch(latestVersion);
    api
      .retryRestart()
      .then((rsp: any) => {
        if (rsp.code !== 0) {
          refused(rsp.msg || t('settings.update.updateFailed'));
          return;
        }
        if (!rsp.data?.restarting) {
          // Nothing to restart any more (it settled meanwhile): show the current state.
          clearWatch();
          setIsLocked(false);
          checkForUpdates();
          return;
        }
        watchRestart(latestVersion); // the pending version
      })
      .catch((error: any) => {
        const code = error?.response?.status;
        if (code && ![502, 503, 504].includes(code)) {
          refused(`${t('settings.update.updateFailed')} (HTTP ${code})`); // refused outright
          return;
        }
        // No answer, or a gateway error: the retry may have been accepted. Find out by watching.
        watchRestart(latestVersion);
      });
  }

  // akkijp fork: after an update or a restart retry the device restarts, and may reboot (a new
  // HDMI driver or init script). Watch the version API until it answers again and the update has
  // settled, then reload; never reload into a page that cannot load.
  // Before an upload, make sure the version and server start time it will be compared against
  // are known (the first query may have failed); without them the result could never be
  // confirmed. Resolves to false if the device cannot be reached now.
  // Always queried afresh right before a request: a version or start time from when the page
  // was opened may be stale (another tab may have updated meanwhile).
  function ensureBaseline(): Promise<boolean> {
    return api
      .getVersion()
      .then((rsp: any) => {
        const data = rsp?.data;
        if (rsp?.code !== 0 || !data?.serverStarted) return false;
        baselineRef.current = { version: data.current || '', started: data.serverStarted };
        return true;
      })
      .catch(() => false);
  }

  // Store the watch (target, version and start time before the request) as soon as the request is
  // about to go out, so that a page reload or re-login while the device installs can resume it.
  function rememberWatch(target: string) {
    saveWatch({
      target,
      previous: baselineRef.current.version,
      startedFrom: baselineRef.current.started,
      until: Date.now() + 10 * 60 * 1000
    });
  }

  function watchRestart(target: string) {
    // A reply that arrives after the tab was closed must not lock the settings again.
    if (!mountedRef.current) return;
    saveWatch({
      target,
      previous: baselineRef.current.version,
      startedFrom: baselineRef.current.started,
      until: Date.now() + 10 * 60 * 1000
    });
    watchTargetRef.current = target;
    watchStartedFromRef.current = baselineRef.current.started;
    watchSawPendingRef.current = false;
    watchPreviousRef.current = baselineRef.current.version;
    setIsLocked(true);
    setStatus('updating');
    setErrMsg('');
    reloadWhenSettledRef.current = true;
    watchUntilRef.current = Date.now() + 10 * 60 * 1000;
    scheduleRecheck();
  }

  async function update() {
    if (status !== 'outdated') return;

    setIsLocked(true);
    setStatus('updating');
    if (!(await ensureBaseline()) || !mountedRef.current) {
      setIsLocked(false);
      if (mountedRef.current) {
        setStatus('failed');
        setErrMsg(t('settings.update.queryFailed'));
      }
      return;
    }

    rememberWatch(latestVersion); // before the POST: a reload during the install can resume it
    api
      .update(latestVersion)
      .then((rsp: any) => {
        if (rsp.code === -5) {
          // A newer release appeared since the page was loaded: nothing was installed. Show it.
          clearWatch();
          setIsLocked(false);
          checkForUpdates();
          return;
        }
        if (rsp.code !== 0) {
          showFailure(rsp.msg || t('settings.update.updateFailed'));
          return;
        }
        watchRestart(latestVersion);
      })
      .catch((error: any) => {
        const code = error?.response?.status;
        if (code && ![502, 503, 504].includes(code)) {
          // Refused outright (e.g. by a proxy): nothing started. Say so and unlock.
          clearWatch();
          setIsLocked(false);
          setStatus('failed');
          setErrMsg(`${t('settings.update.updateFailed')} (HTTP ${code})`);
          return;
        }
        // No answer, or a gateway error: the server may already be restarting into the new
        // version. Find out by watching.
        watchRestart(latestVersion);
      });
  }

  return (
    <>
      <div className="text-base">{t('settings.update.title')}</div>
      <Divider className="opacity-50" />

      {/* akkijp fork: no preview channel and no custom update server; updates come only from the fork's releases. */}
      {/* akkijp fork: while an earlier update is unsettled the server refuses new installs; hide the
          offline upload so the retry button or recovery note stays on screen. It stays mounted
          during "updating": its own upload sets that status and must not be torn down by it (the
          upload itself refuses to start while another update is running). */}
      {!['restartPending', 'switchIncomplete', 'confirming'].includes(status) && (
        <>
          <Offline
            status={status}
            setStatus={setStatus}
            setIsLocked={setIsLocked}
            setErrMsg={setErrMsg}
            onInstalled={watchRestart}
            onFailed={showFailure}
            onSending={rememberWatch}
            ensureBaseline={ensureBaseline}
          />
          <Divider className="opacity-50" />
        </>
      )}

      {/* akkijp fork: a microphone that could not be disabled is shown even though the KVM runs. */}
      {picoclaw.startsWith('FAILED') && (
        <Alert className="mb-4" type="error" showIcon message={t('settings.update.picoclawFailed')} description={picoclaw} />
      )}
      {microphone.startsWith('FAILED') && (
        <Alert className="mb-4" type="error" showIcon message={t('settings.update.microphoneFailed')} description={microphone} />
      )}

      <div className="flex min-h-[320px] flex-col justify-between">
        {status === 'loading' && (
          <div className="flex justify-center pt-24">
            <Spin indicator={<LoadingOutlined spin />} size="large" />
          </div>
        )}

        {status === 'updating' && (
          <div className="flex flex-col items-center justify-center space-y-10 pb-10 pt-24">
            <Spin size="large" />
            <span className="text-neutral-500">{t('settings.update.updating')}</span>
          </div>
        )}

        {status === 'latest' && (
          <Result
            status="success"
            icon={<SmileOutlined />}
            title={currentVersion}
            subTitle={t('settings.update.isLatest')}
            extra={[
              <Button key="confirm" onClick={checkForUpdates}>
                {t('settings.update.title')}
              </Button>
            ]}
          />
        )}

        {status === 'restartPending' && (
          <Result
            status="warning"
            title={latestVersion || currentVersion}
            subTitle={errMsg ? `${t('settings.update.restartPending')} (${errMsg})` : t('settings.update.restartPending')}
            extra={[
              <Button key="retry" type="primary" onClick={retryRestart}>
                {t('settings.update.retryRestart')}
              </Button>
            ]}
          />
        )}

        {status === 'confirming' && (
          <div className="flex flex-col items-center justify-center space-y-10 pb-10 pt-24">
            <Spin size="large" />
            <span className="text-neutral-500">{t('settings.update.confirming')}</span>
          </div>
        )}

        {status === 'switchIncomplete' && (
          <Result status="error" title={currentVersion} subTitle={t('settings.update.switchIncomplete')} />
        )}

        {status === 'outdated' && (
          <Result
            status="warning"
            icon={<RocketOutlined />}
            title={`${currentVersion} -> ${latestVersion}`}
            subTitle={t('settings.update.available')}
            extra={[
              <Button
                key="confirm"
                type="primary"
                disabled={false}
                onClick={update}
              >
                {t('settings.update.confirm')}
              </Button>
            ]}
          />
        )}

        {status === 'failed' && <Result subTitle={errMsg} />}

        <div className="flex justify-center">
          <Button
            type="link"
            size="small"
            href="https://github.com/sipeed/NanoKVM/blob/main/CHANGELOG.md"
            target="_blank"
          >
            CHANGELOG
          </Button>
        </div>
      </div>
    </>
  );
};
