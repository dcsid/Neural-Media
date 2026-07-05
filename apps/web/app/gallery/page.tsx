"use client";

// The gallery moved to the landing page (/). This stub keeps old links and
// bookmarks working — a client-side replace because the site ships as a
// static export (no server redirects), with a plain link as the no-JS
// fallback.

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function GalleryMovedRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/");
  }, [router]);
  return (
    <main className="mx-auto max-w-[1280px] px-8 pb-16 pt-12">
      <p className="text-[13px] text-ink-300">
        The gallery is now the home page —{" "}
        <Link
          href="/"
          className="text-accent underline underline-offset-2 transition-opacity hover:opacity-80"
        >
          continue to the gallery
        </Link>
        .
      </p>
    </main>
  );
}
