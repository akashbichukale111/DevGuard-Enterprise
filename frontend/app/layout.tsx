import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DevGuard AI",
  description: "Enterprise AI-powered code security scanner",
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
