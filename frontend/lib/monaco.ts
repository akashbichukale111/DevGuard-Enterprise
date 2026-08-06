// Point @monaco-editor/react at the copy of Monaco served from this origin.
//
// Without this the loader defaults to
// `https://cdn.jsdelivr.net/npm/monaco-editor@<version>/min/vs`, so every
// screen with an editor depends on a third-party CDN being reachable at the
// moment a judge opens it. `scripts/copy-monaco.mjs` stages that same bundle
// into `public/monaco/vs` during the build; this points the loader there.
//
// Import this module for its side effect from every entry point that mounts an
// editor, before the editor renders. The call is idempotent and must happen
// before the first mount — @monaco-editor/loader ignores config once loading
// has started, so a late import silently falls back to the CDN.

import { loader } from "@monaco-editor/react";

// The version is pinned in package.json rather than interpolated here: the
// files under public/monaco/vs are whatever `npm ci` resolved, and the loader
// only needs the path.
loader.config({ paths: { vs: "/monaco/vs" } });

export { loader };
