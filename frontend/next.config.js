/** @type {import('next').NextConfig} */

/**
 * Two build modes, because two things need to ship and they need opposite
 * outputs.
 *
 *   * default (`standalone`) — what frontend/Dockerfile expects: a
 *     self-contained server.js with only the node_modules actually reachable at
 *     runtime, which is what keeps the runtime image from needing the full
 *     dependency tree.
 *   * `NEXT_OUTPUT=export` — a fully static site for zero-infrastructure replay's replay URL. The
 *     Command Center fetches its bundles from `public/replay/` in the browser
 *     and no route does data access on the server, so the whole thing renders
 *     from files on any static host, with no DataHub, no key and no database.
 *
 * `images.unoptimized` is required under `export`: the default image optimiser
 * is a server feature, and leaving it on makes the export fail rather than
 * silently degrade.
 */
const isExport = process.env.NEXT_OUTPUT === 'export';

/**
 * The commit this bundle was built from, surfaced to the client.
 *
 * A reviewer opening a deployed replay URL has no other way to confirm the
 * build in front of them is the commit that was submitted. Vercel, GitHub
 * Actions, Netlify and Heroku each export the SHA under their own name;
 * falling back to the local git HEAD keeps a developer build honest rather
 * than blank. Empty when nothing is available, and the footer then says
 * "unknown" instead of inventing one.
 */
function resolveCommitSha() {
  const fromCi =
    process.env.VERCEL_GIT_COMMIT_SHA ||
    process.env.GITHUB_SHA ||
    process.env.COMMIT_REF ||        // Netlify
    process.env.SOURCE_VERSION ||    // Heroku
    '';
  if (fromCi) return fromCi;
  try {
    return require('child_process')
      .execSync('git rev-parse HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString()
      .trim();
  } catch {
    return '';
  }
}

const nextConfig = {
  output: isExport ? 'export' : 'standalone',
  env: { NEXT_PUBLIC_COMMIT_SHA: resolveCommitSha() },
  ...(isExport
    ? {
        images: { unoptimized: true },
        // Emits `command/index.html` rather than `command.html`, so a plain
        // static host serves /command without needing rewrite rules.
        trailingSlash: true,
      }
    : {}),
};

module.exports = nextConfig;
