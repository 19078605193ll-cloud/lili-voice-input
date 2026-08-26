import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const targetArgument = process.argv[2];
if (!targetArgument) {
  throw new Error("Usage: npm run export:vendor -- <target-directory>");
}
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = resolve(root, "packages/browser/dist");
const targetRoot = resolve(process.cwd(), targetArgument);
await mkdir(targetRoot, { recursive: true });
for (const file of ["lili-voice-input.global.js", "lili-voice-input.global.js.map", "pcm-worklet.js"]) {
  await copyFile(resolve(sourceRoot, file), resolve(targetRoot, file));
}
process.stdout.write(`Exported browser SDK assets to ${targetRoot}\n`);

