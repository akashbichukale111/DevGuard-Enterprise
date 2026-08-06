// Stage Monaco's AMD bundle into public/ so the editor loads from this origin.
//
// @monaco-editor/react does not bundle Monaco. Its loader fetches
// `monaco-editor@<version>/min/vs/loader.js` from the jsdelivr CDN at runtime,
// which means the Scanner's editor and the diff editors on /result and in
// OmniHealPanel silently never mount on any network that cannot reach
// jsdelivr — a proxied corporate network, an air-gapped review machine, or
// anyone running the replay build offline. Observed directly: the editor sits
// on "Loading editor…" forever and the only signal is a failed request in the
// devtools network tab.
//
// Copying `min/vs` into public/ and pointing the loader at it (see
// lib/monaco.ts) makes the editor a first-party asset with no external
// dependency at runtime.
//
// This runs from npm's `prebuild` hook, so it happens automatically on both
// `npm run build` and the deploy build. The output is gitignored — it is a
// build artifact reproduced from node_modules, not source.

import { cp, rm, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = join(here, "..");
const src = join(frontend, "node_modules", "monaco-editor", "min", "vs");
const dest = join(frontend, "public", "monaco", "vs");

try {
  await stat(src);
} catch {
  console.error(
    `[copy-monaco] monaco-editor not found at ${src}\n` +
      `[copy-monaco] Run 'npm ci' first. The editor will not load without it.`
  );
  process.exit(1);
}

await rm(dest, { recursive: true, force: true });
await cp(src, dest, { recursive: true });

console.log(`[copy-monaco] staged ${src} -> ${dest}`);
