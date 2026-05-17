import type { Metadata } from "next";
import "./globals.css";
import { Header } from "@/components/Header";
import { Footer } from "@/components/Footer";
import { MockModeBadge } from "@/components/MockModeBadge";

export const metadata: Metadata = {
  title: "Neural Media",
  description:
    "Predicted average cortical fMRI response to your TikTok watch history. Local-first. Non-commercial. Built on Meta FAIR TRIBE v2.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen antialiased">
        <a href="#main" className="skip-link">
          Skip to main content
        </a>
        <div className="flex min-h-screen flex-col">
          <Header />
          {/* Mock-mode honesty banner. Renders only when at least one
              complete InferenceRun in the catalogue used a mock backend;
              hidden in real-mode and on a fresh catalogue. */}
          <MockModeBadge />
          <div id="main" className="flex-1">
            {children}
          </div>
          <Footer />
        </div>
      </body>
    </html>
  );
}
