import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-8 grid gap-5 border-b border-line pb-7 md:grid-cols-[1fr_auto] md:items-end">
      <div>
        <p className="section-label">{eyebrow}</p>
        <h1 className="mt-3 max-w-4xl text-3xl font-semibold leading-[1.05] tracking-[-0.035em] text-foreground sm:text-5xl">
          {title}
        </h1>
        <p className="mt-4 max-w-2xl text-sm leading-6 text-muted sm:text-base">{description}</p>
      </div>
      {actions}
    </header>
  );
}
