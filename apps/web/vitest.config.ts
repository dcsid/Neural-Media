import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest harness for the web app. Path aliases mirror tsconfig.json
// (`@/*` → apps/web root, `@shared/*` → the shared contract package) so
// imports resolve identically under test and under `next build`.
const webRoot = fileURLToPath(new URL("./", import.meta.url));
const sharedRoot = fileURLToPath(new URL("../../shared/", import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: /^@shared\//, replacement: sharedRoot },
      { find: /^@\//, replacement: webRoot },
    ],
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["__tests__/**/*.test.{ts,tsx}", "lib/**/*.test.ts"],
    exclude: ["node_modules", ".next"],
    restoreMocks: true,
  },
});
