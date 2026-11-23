// akkijp fork: the update page, rendered. Guards the offline upload end to end: a package chosen
// with its SHA-256 is POSTed exactly once, even while the page re-reads the device's version
// (the status switches to "updating" meanwhile), and the restart is then watched.
import { fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

import * as api from '@/api/application.ts';

import { Update } from './index.tsx';

// The real offlineUpdate (it calls fetch, checked below); the version queries are faked.
vi.mock('@/api/application.ts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/application.ts')>()),
  getVersion: vi.fn(),
  update: vi.fn(),
  retryRestart: vi.fn()
}));

beforeEach(() => {
  // antd needs matchMedia, which jsdom lacks.
  window.matchMedia =
    window.matchMedia ||
    ((query: string) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false
      }) as unknown as MediaQueryList);
  sessionStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

test('a chosen package is sent once with its SHA-256 and its restart is watched', async () => {
  const versions = [
    { code: 0, data: { current: '2.5.4.1', latest: '2.5.4.1', serverStarted: 100 } } // page opened
  ];
  let releaseBaseline: () => void = () => {};
  vi.mocked(api.getVersion)
    .mockImplementationOnce(() => Promise.resolve(versions[0]) as never)
    // The baseline query right before the upload answers late: the page shows "updating" by then.
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseBaseline = () => resolve(versions[0]);
        }) as never
    )
    .mockImplementation(() => new Promise(() => {}) as never); // later watch queries: pending
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ code: 0 }), { status: 200 })
  );
  vi.stubGlobal('fetch', fetchMock);

  const setIsLocked = vi.fn();
  const { container } = render(<Update setIsLocked={setIsLocked} />);
  await waitFor(() => expect(api.getVersion).toHaveBeenCalledTimes(1));

  const sha256 = 'a'.repeat(64);
  const checksumInput = container.querySelector('input:not([type="file"])') as HTMLInputElement;
  fireEvent.change(checksumInput, { target: { value: sha256 } });
  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['package'], 'nanokvm_2.5.4.2.tar.gz');
  fireEvent.change(fileInput, { target: { files: [file] } });

  await waitFor(() => expect(api.getVersion).toHaveBeenCalledTimes(2));
  expect(fetchMock).not.toHaveBeenCalled(); // waits for the baseline
  // The watch is stored before the upload goes out (a reload during the install can resume it).
  releaseBaseline();

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toMatch(/\/api\/application\/update\/offline$/);
  expect(init.method).toBe('POST');
  expect(init.headers['X-SHA256-Checksum']).toBe(sha256);
  expect((init.body as FormData).get('file')).toBe(file);

  // Accepted: the restart into 2.5.4.2 is watched (and remembered across a re-login).
  await waitFor(() =>
    expect(JSON.parse(sessionStorage.getItem('nanokvm-update-watch') || '{}')).toMatchObject({
      target: '2.5.4.2',
      previous: '2.5.4.1',
      startedFrom: 100
    })
  );
  expect(setIsLocked).toHaveBeenLastCalledWith(true);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test('an update refused because a newer release appeared leaves nothing to resume', async () => {
  const outdated = {
    code: 0,
    data: { current: '2.5.4.1', latest: '2.5.4.2', serverStarted: 100 }
  };
  vi.mocked(api.getVersion).mockResolvedValue(outdated as never);
  vi.mocked(api.update).mockResolvedValue({ code: -5, msg: 'the latest version changed' } as never);

  const setIsLocked = vi.fn();
  const { findByText, unmount } = render(<Update setIsLocked={setIsLocked} />);
  fireEvent.click(await findByText('settings.update.confirm'));

  await waitFor(() => expect(api.update).toHaveBeenCalledWith('2.5.4.2'));
  await waitFor(() => expect(setIsLocked).toHaveBeenLastCalledWith(false));
  expect(sessionStorage.getItem('nanokvm-update-watch')).toBeNull();

  // Opening the page again does not resume a watch for the update that never started.
  unmount();
  setIsLocked.mockClear();
  render(<Update setIsLocked={setIsLocked} />);
  await waitFor(() => expect(api.getVersion).toHaveBeenCalled());
  expect(setIsLocked).not.toHaveBeenCalledWith(true);
});
