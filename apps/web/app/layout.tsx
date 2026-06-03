import type { Metadata } from "next";
import "./globals.css";
import { Header } from "@/components/Header";
import { Footer } from "@/components/Footer";

export const metadata: Metadata = {
  title: "Neural Media",
  description:
    "Predicted average cortical fMRI response to a short YouTube video segment. Paste a URL, pick a clip, watch the brain light up. Non-commercial. Built on Meta FAIR TRIBE v2.",
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
          <div id="main" className="flex-1">
            {children}
          </div>
          <Footer />
        </div>
      </body>
    </html>
  );
}
