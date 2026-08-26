import { defineConfig } from "vite";

export default defineConfig({
  base: "/demo/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});

