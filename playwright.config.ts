import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './tests/ui',
  use: {
    baseURL: process.env.DASHBOARD_BASE_URL || 'http://localhost:8080',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
