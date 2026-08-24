"use client";

import { Activity, Bot, Cable, ChartNoAxesCombined, GitBranch, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const navigation = [
  { href: "/", label: "Sistema", icon: Activity },
  { href: "/connectors", label: "Conectores", icon: Cable },
  { href: "/playground", label: "Playground", icon: Bot },
  { href: "/evaluations", label: "Avaliações", icon: ChartNoAxesCombined },
  { href: "/trace", label: "Trace", icon: GitBranch },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[248px_1fr]">
      <aside className="sidebar-grid border-b border-line bg-ink/95 lg:sticky lg:top-0 lg:h-screen lg:border-r lg:border-b-0">
        <div className="flex h-full flex-col px-4 py-4 lg:px-5 lg:py-6">
          <Link href="/" className="group flex items-center gap-3 px-2" aria-label="IndusGuard — início">
            <span className="grid size-10 place-items-center border border-signal/55 bg-signal/10 text-signal transition-colors group-hover:bg-signal group-hover:text-ink">
              <ShieldCheck size={21} strokeWidth={1.8} />
            </span>
            <span>
              <strong className="block text-[15px] font-semibold tracking-[0.12em]">INDUSGUARD</strong>
              <span className="font-mono text-[9px] uppercase tracking-[0.2em] text-muted">control plane</span>
            </span>
          </Link>

          <nav className="mt-5 grid grid-cols-5 gap-1 pb-1 lg:mt-12 lg:block lg:space-y-1" aria-label="Principal">
            {navigation.map((item, index) => {
              const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "group flex min-w-0 items-center justify-center gap-1.5 border-l-2 px-1 py-2.5 text-xs transition-colors sm:gap-2 sm:px-3 sm:text-sm lg:justify-start lg:gap-3",
                    active
                      ? "border-signal bg-signal/[0.08] text-foreground"
                      : "border-transparent text-muted hover:border-line-bright hover:bg-white/[0.025] hover:text-foreground",
                  )}
                >
                  <span className="hidden font-mono text-[9px] text-dim lg:inline">0{index + 1}</span>
                  <Icon size={16} strokeWidth={1.7} />
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="mt-auto hidden border-t border-line pt-5 lg:block">
            <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-dim">Modo público</p>
            <p className="mt-2 text-xs leading-relaxed text-muted">
              Leituras públicas e playground owner-only. Escritas sempre simuladas.
            </p>
          </div>
        </div>
      </aside>
      <main className="relative min-w-0 overflow-hidden">
        <div className="scanline" aria-hidden="true" />
        <div className="mx-auto w-full max-w-[1500px] px-4 py-7 sm:px-7 lg:px-10 lg:py-10">{children}</div>
      </main>
    </div>
  );
}
