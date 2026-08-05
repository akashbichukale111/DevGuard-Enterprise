// ESLint flat config — required by the Next 14 -> 16 upgrade.
//
// Two breaking changes forced this file into existence, and neither is optional:
//
//   1. `next lint` was REMOVED in Next 16. It now parses its argument as a
//      directory, so the old `"lint": "next lint"` script failed with
//      `Invalid project directory provided, no such directory: .../frontend/lint`.
//      Linting goes through the `eslint` CLI directly now, hence
//      `"lint": "eslint ."` in package.json.
//
//   2. `eslint-config-next@16` requires `eslint>=9`, and ESLint 9 reads
//      `eslint.config.*` rather than `.eslintrc.json` (now deleted). The old
//      `{ "extends": "next/core-web-vitals" }` becomes a spread of the flat
//      array that `eslint-config-next` exports from its subpath.
//
// The BASE rule set is deliberately unchanged: `core-web-vitals` is exactly what
// `.eslintrc.json` extended.
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";

// ---------------------------------------------------------------------------
// RULES THAT ARE NEW IN THIS ECOSYSTEM BUMP, DEMOTED TO WARNINGS.
//
// eslint-config-next 16 ships a much newer eslint-plugin-react-hooks and
// @next/eslint-plugin-next than the 14.x line did. Four rules that did not
// EXIST under the old config now fire on code this upgrade did not touch:
//
//   react-hooks/set-state-in-effect   app/page.tsx:54, :68
//                                     app/result/page.tsx:378
//                                     app/scanner/page.tsx:143
//   react-hooks/refs                  app/page.tsx:61
//   react-hooks/immutability          app/result/page.tsx:340
//   @next/next/no-html-link-for-pages app/result/page.tsx:287
//
// These are NOT regressions from the upgrade — every one of them is a
// pre-existing pattern that the old rule set simply did not check. Leaving them
// as errors would fail `npm run lint` and redden CI for a dependency bump that
// changed no application code; "fixing" seven UI call sites inside a dependency
// commit would break both "smallest possible modification" and "verify the
// application behaves identically", and none of them can be verified end to end
// without a live API key.
//
// So they are demoted to `warn`: still printed on every lint run, still visible
// in CI output, not silenced with a disable comment, and tracked in
// SECURITY.md as follow-up work. Two are worth a real look when someone picks
// them up:
//
//   * `no-html-link-for-pages` at result/page.tsx:287 is a genuine (minor) UX
//     issue that predates all of this — a bare `<a href="/">` to an internal
//     route does a full page reload instead of a client-side navigation.
//   * `react-hooks/refs` at page.tsx:61 flags `doneRef.current = onDone` during
//     render, which is a real React anti-pattern even though it is benign here.
//
// The other five are cascading-render style findings, not defects. Promote any of
// these back to `error` in the same commit that fixes its call sites.
// ---------------------------------------------------------------------------
const newRulesFromTheNext16Bump = {
  rules: {
    "react-hooks/set-state-in-effect": "warn",
    "react-hooks/refs": "warn",
    "react-hooks/immutability": "warn",
    "@next/next/no-html-link-for-pages": "warn",
  },
};

const config = [
  // Build output and dependencies. `.eslintrc.json` never needed this — ESLint 8
  // ignored `node_modules` by default and `next lint` scoped itself to the app
  // directories. The flat CLI lints the whole tree unless told otherwise, so
  // without this it would walk `.next/` and report on generated code.
  {
    ignores: [".next/**", "node_modules/**", "out/**", "next-env.d.ts"],
  },
  ...nextCoreWebVitals,
  newRulesFromTheNext16Bump,
];

export default config;
