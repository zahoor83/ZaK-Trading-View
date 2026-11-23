import { defineConfig, devices } from "@playwright/test";

const baseURL = "http://localhost:3000";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: "list",
  timeout: 60_000,

  expect: {
    timeout: 10_000,
  },

  use: {
    baseURL,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
      },
    },
  ],

  webServer: [
    {
      command: "node tests/e2e/mock-api-server.mjs",
      url: "http://127.0.0.1:8000/__health",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        ...process.env,
        OIDC_INTERNAL_SERVICE_SECRET:
          "local-e2e-only-secret-never-use-in-production-2026",
      },
    },
    {
      command: "npx next start --hostname 127.0.0.1 --port 3000",
      url: `${baseURL}/login`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        ...process.env,
        APP_ENV: "development",
        APP_PUBLIC_URL: baseURL,
        AUTH0_DOMAIN: "example.auth0.com",
        AUTH0_CLIENT_ID: "zak-e2e-client",
        AUTH0_CALLBACK_URL: `${baseURL}/api/auth/oidc/callback`,
        ZAK_MARKET_DATA_API_BASE_URL: "http://127.0.0.1:8000",
        OIDC_INTERNAL_SERVICE_SECRET:
          "local-e2e-only-secret-never-use-in-production-2026",
      },
    },
  ],
});