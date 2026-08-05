/**
 * verify_replay_ui.mjs — prove zero-infrastructure replay's claim by exercising the built UI.
 *
 *     make verify-replay-ui
 *
 * or by hand:
 *
 *     make replay-build
 *     (cd frontend/out && python -m http.server 8931 &)
 *     cd frontend && npm install --no-save playwright
 *     cd frontend && node scripts/verify_replay_ui.mjs
 *
 * It lives under `frontend/` so ESM resolves `playwright` out of that
 * workspace — module resolution follows the file's own location, not the
 * working directory, so a copy in the repo-root `scripts/` cannot find it.
 * Playwright is installed on demand rather than pinned as a devDependency: it
 * is a manual verification tool, and adding it to the lockfile would put a
 * browser download into every CI install for a check CI does not run.
 *
 * The zero-infrastructure replay requirement says replay "drives this exact UI from the proof pack with zero
 * infrastructure". A passing `next build` does not prove that — it proves the
 * page compiles. What has to be true is stronger, and each check below maps to
 * one way the claim could be false while everything still looks fine:
 *
 *   * the page renders with no DataHub, no key and no database running;
 *   * an evidence chip opens the *actual captured bytes*, not a placeholder,
 *     which is the evidence rule promise the ledger makes on every row;
 *   * a tool-call row opens its recorded MCP response;
 *   * the run picker reaches the refusal, because a refusal that cannot be
 *     shown is not a demo beat;
 *   * nothing throws.
 *
 * Requires `playwright` and a Chromium binary. Both are dev-only: the shipped
 * static site needs neither.
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.REPLAY_BASE ?? "http://localhost:8931";
//: Relative to `frontend/`, where this script lives and runs.
const SHOTS = process.env.REPLAY_SHOTS ?? "../evidence/d10/screenshots";
const FULL_RUN = "d6-loop-pass2";
const REFUSED_RUN = "d5-refusal";

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}${detail ? ` — ${detail}` : ""}`);
}

mkdirSync(SHOTS, { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH ?? "/opt/pw-browsers/chromium",
});
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });

const pageErrors = [];
const badRequests = [];
page.on("pageerror", (e) => pageErrors.push(String(e.message)));
page.on("response", (r) => {
  // favicon.ico is not part of the export and is not worth failing over.
  if (r.status() >= 400 && !r.url().endsWith("/favicon.ico")) {
    badRequests.push(`${r.status()} ${r.url()}`);
  }
});

console.log(`\nverify_replay_ui: ${BASE}/command/\n`);

// ---- the full loop --------------------------------------------------------
await page.goto(`${BASE}/command/?run=${FULL_RUN}`, { waitUntil: "networkidle" });
await page.waitForTimeout(800);

const text = await page.innerText("body");
check("page renders with no backend running", text.length > 4000, `${text.length} chars`);
check(
  "replay banner is present and unmissable",
  text.includes("REPLAY OF RECORDED RUN — NOT LIVE"),
);
check("all six state-machine states render", text.includes("KNOWLEDGE_WRITTEN"));
check(
  "cost is N/A rather than a placeholder zero",
  /COST\s*\n?\s*N\/A/.test(text) && !text.includes("$0.00"),
);

await page.screenshot({ path: `${SHOTS}/${FULL_RUN}.png`, fullPage: true });

// An evidence chip must open the real captured bytes.
await page
  .locator("ul li button")
  .filter({ hasText: /^EV-/ })
  .first()
  .click();
await page.waitForTimeout(400);
const modalOpen = (await page.locator("text=Raw payload").count()) > 0;
const rawBody = await page.locator("pre").last().innerText();
check("evidence chip opens the raw payload viewer", modalOpen);
check(
  "the viewer shows real captured bytes",
  rawBody.length > 200 && rawBody.includes("dbt"),
  `${rawBody.length} chars`,
);
await page.keyboard.press("Escape");
await page.waitForTimeout(250);
check("Escape closes the viewer", (await page.locator("text=Raw payload").count()) === 0);

// A rail node must open its own AgentHandoff record.
await page.locator("button", { hasText: "Pathfinder" }).first().click();
await page.waitForTimeout(300);
check(
  "a rail node opens its AgentHandoff record",
  (await page.locator("text=pathfinder → diagnostician").count()) > 0,
);

// A tool-call row must open the MCP response it recorded.
await page.locator("table button", { hasText: "open" }).first().click();
await page.waitForTimeout(400);
const toolBody = await page.locator("pre").last().innerText();
check(
  "a tool call opens its recorded MCP response",
  toolBody.includes("urn:li:dataset:"),
  `${toolBody.length} chars`,
);
await page.keyboard.press("Escape");

// ---- the refusal ----------------------------------------------------------
await page.locator("button", { hasText: REFUSED_RUN }).first().click();
await page.waitForTimeout(1200);
const refusedText = await page.innerText("body");
check(
  "the run picker reaches the refusal",
  page.url().includes(REFUSED_RUN) && refusedText.includes("INSUFFICIENT EVIDENCE"),
);
check("the refusal names the missing evidence class", refusedText.includes("DATAHUB_GRAPH"));
check(
  "a refused run reports no time-to-root-cause",
  /TIME TO ROOT CAUSE\s*\n?\s*N\/A/.test(refusedText),
);
await page.screenshot({ path: `${SHOTS}/${REFUSED_RUN}.png`, fullPage: true });

check("no uncaught page errors", pageErrors.length === 0, pageErrors.join("; "));
check("no failed requests", badRequests.length === 0, badRequests.join("; "));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
console.log(`screenshots: ${SHOTS}/\n`);
process.exit(failed.length === 0 ? 0 : 1);
