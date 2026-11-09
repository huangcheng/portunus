import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: proxy the API to a locally running gateway (set PORTUNUS_INSECURE_COOKIE=1 there, since
// the dev server is plain http). Prod: serve dist/ from the same origin as /api.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": process.env.PORTUNUS_GATEWAY ?? "http://127.0.0.1:8080" } },
});
