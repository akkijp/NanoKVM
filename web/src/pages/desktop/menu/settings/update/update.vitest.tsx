// akkijp fork: the update page, rendered. Guards the offline upload end to end: a package chosen
// with its SHA-256 is POSTed exactly once, even while the page re-reads the device's version
// (the status switches to "updating" meanwhile), and the restart is then watched.
import { fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

import * as api from '@/api/application.ts';

import { Update } from './index.tsx';

vi.mock('@/api/application.ts', () => ({
  getVersion: vi.fn(),
  offlineUpdate: vi.fn(),
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
  vi.mocked(api.offlineUpdate).mockResolvedValue(
    new Response(JSON.stringify({ code: 0 }), { status: 200 })
  );

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
  expect(api.offlineUpdate).not.toHaveBeenCalled(); // waits for the baseline
  releaseBaseline();

  await waitFor(() => expect(api.offlineUpdate).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.offlineUpdate).mock.calls[0][1]).toBe(sha256);

  // Accepted: the restart into 2.5.4.2 is watched (and remembered across a re-login).
  await waitFor(() =>
    expect(JSON.parse(sessionStorage.getItem('nanokvm-update-watch') || '{}')).toMatchObject({
      target: '2.5.4.2',
      previous: '2.5.4.1',
      startedFrom: 100
    })
  );
  expect(setIsLocked).toHaveBeenLastCalledWith(true);
  expect(api.offlineUpdate).toHaveBeenCalledTimes(1);
});
