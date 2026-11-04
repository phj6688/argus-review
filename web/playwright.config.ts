import { defineConfig, devices } from "@playwright/test";

const PORT = 4317;
const BASE = `http://localhost:${PORT}`;
const GITLAB_SPEC = /[\\/]gitlab-[^\\/]*\.spec\.ts$/;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: { baseURL: BASE, trace: "off" },
  projects: [
    {
      name: "desktop",
      testIgnore: GITLAB_SPEC,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 900 } },
    },
    {
      name: "mobile",
      testIgnore: GITLAB_SPEC,
      use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "gitlab",
      testMatch: GITLAB_SPEC,
      fullyParallel: false,
      use: { ...devices["Desktop Chrome"], baseURL: "http://localhost:4319", viewport: { width: 1280, height: 900 } },
    },
  ],
  webServer: [{
    command: `npm run start -- -p ${PORT}`,
    env: { ARGUS_AUTH_MODE: "proxy" },
    url: BASE,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  }, {
    command: "node e2e/support/mock-api.mjs",
    url: "http://127.0.0.1:4318/health",
    reuseExistingServer: !process.env.CI,
  }, {
    command: "npm run start -- -p 4319",
    env: { ARGUS_AUTH_MODE: "proxy", NEXT_PUBLIC_ARGUS_API: "http://127.0.0.1:4318" },
    url: "http://localhost:4319/trigger",
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  }],
});
