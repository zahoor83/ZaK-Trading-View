import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/transport",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  timeout: 15_000,
  expect: {
    timeout: 5_000,
  },
});
