import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Supply Chain Control Tower",
  description: "AI Supply Chain Disruption Agent — detection, impact, sourcing and execution",
};

const nav = [
  { href: "/", label: "Control Tower" },
  { href: "/operations", label: "Operations" },
  { href: "/suppliers", label: "Suppliers" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">
        <header className="sticky top-0 z-10 border-b border-edge bg-ink/90 backdrop-blur">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
            <Link href="/" className="flex items-center gap-2 font-semibold">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-accent shadow-[0_0_12px_#38bdf8]" />
              Disruption Agent
            </Link>
            <nav className="flex gap-4 text-sm text-muted">
              {nav.map((n) => (
                <Link key={n.href} href={n.href} className="hover:text-slate-100">{n.label}</Link>
              ))}
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
