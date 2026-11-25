import { afterEach, expect, test, vi } from 'vitest';

import { getVersion } from '@/api/application.ts';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// akkijp fork: the local version query really carries local=1 on the wire (the server then
// answers without asking GitHub).
test('getVersion(true) sends local=1', async () => {
  const urls: string[] = [];
  const { http } = await import('@/lib/http.ts');
  const adapter = vi.fn(async (config: any) => {
    const url = new URL(config.url, 'http://kvm.local');
    for (const [k, v] of Object.entries(config.params || {})) url.searchParams.set(k, String(v));
    urls.push(url.pathname + url.search);
    return { data: { code: 0, data: {} }, status: 200, statusText: 'OK', headers: {}, config };
  });
  (http as any).instance.defaults.adapter = adapter;
  await getVersion(true);
  await getVersion();
  expect(urls[0]).toBe('/api/application/version?local=1');
  expect(urls[1]).toBe('/api/application/version');
});
