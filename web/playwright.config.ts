import { defineConfig, devices } from "@playwright/test";

const PORT = 4317;
const BASE = `http://localhost:${PORT}`;

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
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 900 } },
    },
    {
      name: "mobile",
      use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } },
    },
  ],
  webServer: {
    command: `npm run start -- -p ${PORT}`,
    env: { ARGUS_AUTH_MODE: "proxy" },
    url: BASE,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
