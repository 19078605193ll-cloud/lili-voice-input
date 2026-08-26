import { defineConfig } from "vite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  build: {
    lib: {
      entry: resolve(projectRoot, "src/index.ts"),
      name: "LiliVoiceInput",
      formats: ["es", "iife"],
      fileName: (format) => format === "es" ? "index.js" : "lili-voice-input.global.js",
    },
    sourcemap: true,
    emptyOutDir: true,
  },
});
