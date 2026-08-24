import { AlertTriangle, Database, LoaderCircle, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function LoadingState({ label = "Sincronizando instrumentos" }: { label?: string }) {
  return (
    <div className="instrument-panel grid min-h-52 place-items-center p-8 text-center" role="status">
      <div>
        <LoaderCircle className="mx-auto animate-spin text-signal" size={24} />
        <p className="mt-4 font-mono text-xs uppercase tracking-[0.16em] text-muted">{label}</p>
      </div>
    </div>
  );
}

export function ErrorState({
  title = "Leitura indisponível",
  message,
  retry,
  compact = false,
}: {
  title?: string;
  message: string;
  retry?: () => void;
  compact?: boolean;
}) {
  return (
    <div
      className={cn("instrument-panel border-danger/30 bg-danger/[0.035] p-6", compact && "p-4")}
      role="alert"
    >
      <AlertTriangle className="text-danger" size={20} />
      <h2 className="mt-3 text-base font-semibold">{title}</h2>
      <p className="mt-1 max-w-xl text-sm leading-6 text-muted">{message}</p>
      {retry ? (
        <Button className="mt-4" variant="outline" size="sm" onClick={retry}>
          <RefreshCw size={13} /> Tentar novamente
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  message,
  command,
}: {
  title: string;
  message: string;
  command?: string;
}) {
  return (
    <div className="instrument-panel grid min-h-64 place-items-center p-8 text-center">
      <div className="max-w-lg">
        <Database className="mx-auto text-signal" size={28} strokeWidth={1.5} />
        <h2 className="mt-4 text-xl font-semibold">{title}</h2>
        <p className="mt-2 text-sm leading-6 text-muted">{message}</p>
        {command ? <code className="mt-5 inline-block border border-line bg-ink px-4 py-2 text-xs text-signal">{command}</code> : null}
      </div>
    </div>
  );
}
