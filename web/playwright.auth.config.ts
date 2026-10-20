import { defineConfig } from "@playwright/test";

export default defineConfig({
  tsconfig: "./tsconfig.json",
  testDir: "./tests",
  workers: 1,
  reporter: [["list"]],
});
