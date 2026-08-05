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

const nextConfig = {
  output: isExport ? 'export' : 'standalone',
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
