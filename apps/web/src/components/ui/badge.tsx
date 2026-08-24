import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.12em]",
  {
    variants: {
      tone: {
        neutral: "border-line bg-white/[0.03] text-muted",
        good: "border-ok/30 bg-ok/10 text-ok",
        warning: "border-signal/35 bg-signal/10 text-signal-bright",
        danger: "border-danger/35 bg-danger/10 text-danger",
        info: "border-info/35 bg-info/10 text-info",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

type BadgeProps = React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export { Badge, badgeVariants };
