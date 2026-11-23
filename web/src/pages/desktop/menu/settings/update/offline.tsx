import { useEffect, useRef, useState } from 'react';
import { Button, Input } from 'antd';
import { ExternalLinkIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import * as api from '@/api/application.ts';
import { isValidPackageName } from '@/lib/version.ts';

interface UpdateProps {
  status: string;
  setStatus: (status: string) => void;
  setIsLocked: (isClosable: boolean) => void;
  setErrMsg: (msg: string) => void;
  // akkijp fork: called once the package is accepted; the parent watches the restart.
  onInstalled: (version: string) => void;
  // akkijp fork: an upload that was refused or failed; the parent shows what the device is left with.
  onFailed: (message: string) => void;
  // akkijp fork: resolves false when the device's current version cannot be read right now.
  ensureBaseline: () => Promise<boolean>;
}

// A definite refusal from the device (as opposed to a lost connection).
class RefusedError extends Error {}

// nanokvm_2.5.4.2.tar.gz -> 2.5.4.2 (the name was validated before the upload)
function versionOf(filename: string) {
  return filename.replace(/^nanokvm_/, '').replace(/\.tar\.gz$/, '');
}

export const Offline = ({
  status,
  setStatus,
  setIsLocked,
  setErrMsg,
  onInstalled,
  onFailed,
  ensureBaseline
}: UpdateProps) => {
  const { t } = useTranslation();

  const inputRef = useRef<HTMLInputElement | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const [sha256Checksum, setSha256Checksum] = useState('');

  function handleClick() {
    inputRef.current?.click();
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) {
      return;
    }
    e.target.value = '';

    if (!validateFilename(file.name)) {
      setStatus('failed');
      setErrMsg(t('settings.update.offline.invalidName'));
      return;
    }

    upload(file);
  }

  async function upload(file: File | null) {
    if (!file) return;


    const checksum = sha256Checksum.trim();
    // akkijp fork: the SHA-256 from the release's sha256.txt is required.
    if (!/^[a-fA-F0-9]{64}$/.test(checksum)) {
      setStatus('failed');
      setErrMsg(t('settings.update.offline.invalidChecksum'));
      return;
    }

    if (!validateFilename(file.name)) {
      setStatus('failed');
      setErrMsg(t('settings.update.offline.invalidName'));
      return;
    }

    if (status === 'loading' || status === 'updating') {
      return;
    }

    setIsLocked(true);
    setStatus('updating');
    setErrMsg('');

    // The version and start time to compare against, taken now (not when the page was opened),
    // while the settings are locked. Without them the outcome could never be confirmed.
    const ready = await ensureBaseline();
    if (!mountedRef.current) {
      setIsLocked(false); // the page was closed meanwhile: do not upload
      return;
    }
    if (!ready) {
      setIsLocked(false);
      setStatus('failed');
      setErrMsg(t('settings.update.queryFailed'));
      return;
    }

    const formData = new FormData();
    formData.append('file', file);

    api
      .offlineUpdate(formData, checksum)
      .then(async (rsp: Response) => {
        // A gateway error (the old server stopped, or the proxy gave up while the device unpacks):
        // the outcome is unknown, find out by watching.
        if ([502, 503, 504].includes(rsp.status)) return;
        if (!rsp.ok) throw new RefusedError(`HTTP error ${rsp.status}`);

        const rspj = await rsp.json();
        if (rspj.code !== 0) {
          const message = rspj.msg?.includes('sha256 checksum mismatch')
            ? t('settings.update.offline.checksumMismatch')
            : rspj.msg || t('settings.update.offline.updateFailed');
          throw new RefusedError(message);
        }
      })
      .then((result: unknown) => {
        if (result !== undefined) return;
        onInstalled(versionOf(file.name));
      })
      .catch((error: unknown) => {
        if (error instanceof RefusedError) {
          onFailed(error.message); // a definite answer, e.g. a checksum mismatch
          return;
        }
        // The connection was lost: the device may still install it. Watch for the version.
        onInstalled(versionOf(file.name));
      });
  }

  function validateFilename(filename: string) {
    return isValidPackageName(filename);
  }

  return (
    <>
      <div className="mt-8 flex flex-col gap-3">
        <div className="flex items-center justify-between gap-4">
          <div className="flex flex-col space-y-1">
            <div className="flex items-center space-x-2">
              <span>{t('settings.update.offline.title')}</span>

              <a
                className="flex items-center text-neutral-500 hover:text-blue-500"
                href="https://github.com/akkijp/NanoKVM/releases"
                target="_blank"
              >
                <ExternalLinkIcon size={15} />
              </a>
            </div>

            <span className="text-xs text-neutral-500">{t('settings.update.offline.desc')}</span>
          </div>

          <input
            id="file-upload"
            ref={inputRef}
            type="file"
            accept=".tar.gz"
            onChange={handleFileChange}
            className="hidden"
          />
          <Button disabled={status === 'loading' || status === 'updating'} onClick={handleClick}>
            {t('settings.update.offline.upload')}
          </Button>
        </div>

        <Input
          value={sha256Checksum}
          maxLength={64}
          disabled={status === 'updating'}
          placeholder={t('settings.update.offline.checksumPlaceholder')}
          onChange={(event) => setSha256Checksum(event.target.value)}
        />
      </div>
    </>
  );
};
