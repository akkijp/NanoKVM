// akkijp fork: component tests (*.vitest.tsx) run in jsdom with the app's own Vite config.
// The pure-logic tests (*.test.ts) run with node:test (see package.json "test").
import { defineConfig, mergeConfig } from 'vitest/config';

import viteConfig from './vite.config.ts';

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      include: ['src/**/*.vitest.tsx'],
      environment: 'jsdom'
    }
  })
);
