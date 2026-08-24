import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3100",
    trace: "retain-on-failure",
  },
  webServer: process.env.CI
    ? [
        {
          command: "make -C ../.. e2e-api",
          url: "http://127.0.0.1:8765/api/v1/health",
          reuseExistingServer: false,
        },
        {
          command:
            "NEXT_PUBLIC_INDUSGUARD_API_URL=http://127.0.0.1:8765 npm run build && npx serve@14 out -l 3100",
          url: "http://127.0.0.1:3100",
          reuseExistingServer: false,
        },
      ]
    : undefined,
});
