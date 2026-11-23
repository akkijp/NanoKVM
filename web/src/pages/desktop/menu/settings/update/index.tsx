import { useEffect, useRef, useState } from 'react';
import { LoadingOutlined, RocketOutlined, SmileOutlined } from '@ant-design/icons';
import { Button, Divider, Result, Spin } from 'antd';
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
  const versionRequestRef = useRef(0);

  useEffect(() => {
    checkForUpdates();
  }, []);

  function checkForUpdates() {
    const requestId = ++versionRequestRef.current;
    setStatus('loading');

    api
      .getVersion()
      .then((rsp: any) => {
        if (requestId !== versionRequestRef.current) return;
        if (rsp.code !== 0 || !rsp.data) {
          setStatus('failed');
          setErrMsg(t('settings.update.queryFailed'));
          return;
        }

        setCurrentVersion(rsp.data.current);

        // akkijp fork: an installed update waits for its restart; offer to retry it.
        if (rsp.data.restartPending) {
          setStatus('restartPending');
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

  function update() {
    // While a restart is pending the server retries only that restart.
    if (status !== 'outdated' && status !== 'restartPending') return;

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
      <Offline
        status={status}
        setStatus={setStatus}
        setIsLocked={setIsLocked}
        setErrMsg={setErrMsg}
      />
      <Divider className="opacity-50" />

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
            title={currentVersion}
            subTitle={t('settings.update.restartPending')}
            extra={[
              <Button key="retry" type="primary" onClick={update}>
                {t('settings.update.retryRestart')}
              </Button>
            ]}
          />
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
