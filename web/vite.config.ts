// The Command Center: Vite dev server on 127.0.0.1:5173, proxying /api to the Fleet API
// on 127.0.0.1:8765 (meridian api serve). Tests run in jsdom with MSW.
import { defineConfig } from "vitest/config";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } },
  },
  preview: { host: "127.0.0.1", port: 4173 },
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "http://127.0.0.1:5173/" } },
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.tsx", "test/**/*.test.ts"],
    restoreMocks: true,
  },
});
