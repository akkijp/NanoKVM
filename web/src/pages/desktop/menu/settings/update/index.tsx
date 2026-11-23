import { useEffect, useRef, useState } from 'react';
import { LoadingOutlined, RocketOutlined, SmileOutlined } from '@ant-design/icons';
import { Alert, Button, Divider, Result, Spin } from 'antd';
import { useTranslation } from 'react-i18next';

import * as api from '@/api/application.ts';

import { Offline } from './offline.tsx';
import { versionStatus } from '@/lib/version.ts';
import { watchFinished } from '@/lib/update-watch.ts';

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
  const serverStartedRef = useRef(0);
  const watchStartedFromRef = useRef(0);
  const watchSawPendingRef = useRef(false);
  const watchPreviousRef = useRef('');
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
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
        if (rsp.data.serverStarted) serverStartedRef.current = rsp.data.serverStarted;

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
            // Waiting for the user (retry or manual recovery): stop watching, unlock the settings.
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

  function retryRestart() {
    if (status !== 'restartPending') return;

    setIsLocked(true);
    setStatus('updating');

    const refused = (message: string) => {
      // Not restarting: unlock, say why, and keep the retry available.
      setIsLocked(false);
      setStatus('restartPending');
      setErrMsg(message);
    };

    api
      .retryRestart()
      .then((rsp: any) => {
        if (rsp.code !== 0) {
          refused(rsp.msg || t('settings.update.updateFailed'));
          return;
        }
        if (!rsp.data?.restarting) {
          // Nothing to restart any more (it settled meanwhile): show the current state.
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
  function ensureBaseline(): Promise<boolean> {
    if (serverStartedRef.current > 0 && currentVersion) return Promise.resolve(true);
    return api
      .getVersion()
      .then((rsp: any) => {
        const data = rsp?.data;
        if (rsp?.code !== 0 || !data?.serverStarted || !data?.current) return false;
        serverStartedRef.current = data.serverStarted;
        setCurrentVersion(data.current);
        watchPreviousRef.current = data.current;
        return true;
      })
      .catch(() => false);
  }

  function watchRestart(target: string) {
    // A reply that arrives after the tab was closed must not lock the settings again.
    if (!mountedRef.current) return;
    watchTargetRef.current = target;
    watchStartedFromRef.current = serverStartedRef.current;
    watchSawPendingRef.current = false;
    watchPreviousRef.current = currentVersion || watchPreviousRef.current;
    setIsLocked(true);
    setStatus('updating');
    setErrMsg('');
    reloadWhenSettledRef.current = true;
    watchUntilRef.current = Date.now() + 10 * 60 * 1000;
    scheduleRecheck();
  }

  function update() {
    if (status !== 'outdated') return;

    setIsLocked(true);
    setStatus('updating');

    api
      .update(latestVersion)
      .then((rsp: any) => {
        if (rsp.code === -5) {
          // A newer release appeared since the page was loaded: show it before updating.
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
          offline upload so the retry button or recovery note stays on screen. */}
      {!['restartPending', 'switchIncomplete', 'confirming', 'updating'].includes(status) && (
        <>
          <Offline
            status={status}
            setStatus={setStatus}
            setIsLocked={setIsLocked}
            setErrMsg={setErrMsg}
            onInstalled={watchRestart}
            onFailed={showFailure}
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
