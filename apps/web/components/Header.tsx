import Link from "next/link";

const NAV = [
  { href: "/", label: "Gallery" },
  { href: "/predict", label: "Predict" },
  { href: "/about", label: "About" },
] as const;

export function Header() {
  return (
    <header className="border-b border-line">
      <div className="mx-auto flex max-w-[1280px] items-center justify-between px-8 py-5">
        <Link href="/" className="flex items-baseline gap-3">
          <span className="font-serif text-[18px] tracking-tightish text-ink-50">
            Neural Media
          </span>
          <span className="eyebrow">predicted cortical response</span>
        </Link>
        <nav className="flex items-center gap-7 text-[13px] text-ink-200">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="transition-colors hover:text-ink-50"
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
