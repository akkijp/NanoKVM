import { useEffect, useRef, useState } from 'react';
import { LoadingOutlined, RocketOutlined, SmileOutlined } from '@ant-design/icons';
import { Alert, Button, Divider, Result, Spin } from 'antd';
import { useTranslation } from 'react-i18next';

import * as api from '@/api/application.ts';

import { Offline } from './offline.tsx';
import { versionStatus } from '@/lib/version.ts';

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
  const versionRequestRef = useRef(0);
  const recheckTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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
          setStatus('failed');
          setErrMsg(t('settings.update.queryFailed'));
          return;
        }

        setCurrentVersion(rsp.data.current);

        // akkijp fork: an update has not been seen running yet. If it is complete, offer to retry
        // the restart; if it stopped half-way, only manual recovery is safe.
        setMicrophone(rsp.data.microphone || '');
        if (rsp.data.restartPending) {
          setLatestVersion(rsp.data.pendingVersion || '');
          if (rsp.data.restartPhase === 'confirming') {
            // Restarted; the device is checking that both daemons stay up. Look again shortly.
            setStatus('confirming');
            if (mountedRef.current) {
              if (recheckTimerRef.current) clearTimeout(recheckTimerRef.current);
              recheckTimerRef.current = setTimeout(checkForUpdates, 5000);
            }
          } else {
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
        if (requestId !== versionRequestRef.current) return;
        setStatus('failed');
        setErrMsg(t('settings.update.queryFailed'));
      });
  }

  function retryRestart() {
    if (status !== 'restartPending') return;

    setIsLocked(true);
    setStatus('updating');

    api
      .retryRestart()
      .then((rsp: any) => {
        if (rsp.code !== 0) {
          setStatus('failed');
          setErrMsg(rsp.msg || t('settings.update.updateFailed'));
        }
      })
      .finally(() => {
        setTimeout(() => {
          setIsLocked(false);
          setErrMsg('');
          window.location.reload();
        }, 12000);
      });
  }

  function update() {
    if (status !== 'outdated') return;

    setIsLocked(true);
    setStatus('updating');

    api
      .update()
      .then((rsp: any) => {
        if (rsp.code !== 0) {
          setStatus('failed');
          setErrMsg(t('settings.update.updateFailed'));
        }
      })
      .finally(() => {
        setTimeout(() => {
          setIsLocked(false);
          setErrMsg('');

          window.location.reload();
        }, 12000);
      });
  }

  return (
    <>
      <div className="text-base">{t('settings.update.title')}</div>
      <Divider className="opacity-50" />

      {/* akkijp fork: no preview channel and no custom update server; updates come only from the fork's releases. */}
      {/* akkijp fork: while an earlier update is unsettled the server refuses new installs; hide the
          offline upload so the retry button or recovery note stays on screen. */}
      {!['restartPending', 'switchIncomplete', 'confirming'].includes(status) && (
        <>
          <Offline
            status={status}
            setStatus={setStatus}
            setIsLocked={setIsLocked}
            setErrMsg={setErrMsg}
          />
          <Divider className="opacity-50" />
        </>
      )}

      {/* akkijp fork: a microphone that could not be disabled is shown even though the KVM runs. */}
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
            subTitle={t('settings.update.restartPending')}
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
