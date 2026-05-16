import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Neural Media",
  description:
    "Predicted average cortical fMRI response to your TikTok watch history. Local-first. Non-commercial. Built on Meta FAIR TRIBE v2.",
};

// SCAFFOLD STUB.
// Owner: frontend-dashboard worker. See docs/worker-briefs/frontend-dashboard.md.
// Replace with header + nav + footer per the design direction.
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
