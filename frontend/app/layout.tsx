import type { Metadata, Viewport } from "next";
import "./globals.css";

// The description used to read "Enterprise AI-powered code security scanner",
// which described one of the three modules and was the text every shared link
// and search result showed for the whole platform.
//
// No Open Graph image is declared. An OG image needs an absolute URL, and this
// build is deployed to whatever host the operator points at it — inventing a
// domain here would produce a broken preview everywhere except that one guess.
// Title and description alone still render a correct card.
const DESCRIPTION =
  "A closed-loop, governed incident agent for data platforms. Proves root cause " +
  "and blast radius from the DataHub graph, fixes under an owner-routed policy " +
  "gate, verifies recovery, then writes verified incident knowledge back into " +
  "the catalog.";

export const metadata: Metadata = {
  // Per-page titles slot into the template; the platform entry uses the default.
  title: {
    default: "DevGuard — governed incident agent for data platforms",
    template: "%s · DevGuard",
  },
  description: DESCRIPTION,
  applicationName: "DevGuard",
  keywords: [
    "DataHub",
    "data lineage",
    "incident response",
    "AI agents",
    "MCP",
    "OpenTelemetry",
    "SigNoz",
    "data platform",
  ],
  openGraph: {
    type: "website",
    siteName: "DevGuard",
    title: "DevGuard — governed incident agent for data platforms",
    description: DESCRIPTION,
  },
  twitter: {
    card: "summary",
    title: "DevGuard — governed incident agent for data platforms",
    description: DESCRIPTION,
  },
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Matches the --dg-void page background, so mobile browser chrome does not
  // frame a dark instrument panel in white.
  themeColor: "#05050a",
  colorScheme: "dark",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
