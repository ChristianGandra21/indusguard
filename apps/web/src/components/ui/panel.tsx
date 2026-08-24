import type * as React from "react";

import { cn } from "@/lib/utils";

function Panel({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("instrument-panel", className)} {...props} />;
}

function PanelHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("border-b border-line px-5 py-4", className)} {...props} />;
}

function PanelTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2 className={cn("font-mono text-xs font-semibold uppercase tracking-[0.18em]", className)} {...props} />
  );
}

function PanelContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-5", className)} {...props} />;
}

export { Panel, PanelContent, PanelHeader, PanelTitle };
