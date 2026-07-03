import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Bundle is served by FastMCP under /console. Base path must match so all
// asset URLs resolve when the app is not on /.
export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // devclaw ships as a Python package; a couple of small chunks are easier
    // to reason about than tree-shaken bundle splitting we don't yet need.
    rollupOptions: {
      output: { manualChunks: undefined },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // dev-server proxy so `vite dev` can hit a running devclaw MCP for data.
      "/projects.json": "http://127.0.0.1:8000",
      "/goals.json": "http://127.0.0.1:8000",
    },
  },
});
