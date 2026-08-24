"use client";

import { useQuery } from "@tanstack/react-query";
import { Check, ChevronRight, LockKeyhole, RotateCcw, ShieldAlert } from "lucide-react";
import { useMemo, useState } from "react";

import { EmptyState, ErrorState, LoadingState } from "@/components/data-state";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader, PanelTitle } from "@/components/ui/panel";
import { ApiError, api } from "@/lib/api";
import type { OperationSummary } from "@/lib/schemas";
import { cn } from "@/lib/utils";

type Filter = "all" | "read" | "write";

export default function ConnectorsPage() {
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: api.connectors });
  const [selectedId, setSelectedId] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const effectiveSelectedId = selectedId || connectors.data?.[0]?.id || "";

  const operations = useQuery({
    queryKey: ["operations", effectiveSelectedId],
    queryFn: () => api.operations(effectiveSelectedId),
    enabled: Boolean(effectiveSelectedId),
  });
  const visibleOperations = useMemo(
    () => (operations.data ?? []).filter((operation) => filter === "all" || operation.access === filter),
    [filter, operations.data],
  );

  if (connectors.isLoading) return <LoadingState label="Carregando catálogo OpenAPI" />;
  if (connectors.error) {
    return (
      <ErrorState
        title={connectors.error instanceof ApiError && connectors.error.code === "API_UNREACHABLE" ? "Backend fora de alcance" : "Falha ao ler catálogo"}
        message={connectors.error.message}
        retry={() => void connectors.refetch()}
      />
    );
  }
  if (!connectors.data?.length) {
    return <EmptyState title="Nenhum conector" message="O catálogo iniciou sem integrações válidas." />;
  }

  const selected = connectors.data.find((connector) => connector.id === effectiveSelectedId);

  return (
    <>
      <PageHeader
        eyebrow="Catálogo / OpenAPI"
        title="Capacidades declaradas. Permissões explícitas."
        description="Cada conector combina contrato técnico, perfil de segurança e linguagem de domínio. Uma operação nova nasce desabilitada até ser configurada."
        actions={<Badge tone="info">{connectors.data.length} conectores</Badge>}
      />

      <div className="grid gap-6 xl:grid-cols-[310px_1fr]">
        <aside>
          <p className="section-label mb-3">Selecione o conector</p>
          <div className="space-y-2">
            {connectors.data.map((connector) => {
              const active = connector.id === effectiveSelectedId;
              return (
                <button
                  key={connector.id}
                  type="button"
                  onClick={() => setSelectedId(connector.id)}
                  className={cn(
                    "w-full border px-4 py-4 text-left transition-colors",
                    active ? "border-signal/55 bg-signal/[0.07]" : "border-line bg-panel hover:border-line-bright",
                  )}
                  aria-pressed={active}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="font-semibold">{connector.name}</p>
                      <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-dim">{connector.auth_type.replaceAll("_", " ")}</p>
                    </div>
                    <ChevronRight className={active ? "text-signal" : "text-dim"} size={16} />
                  </div>
                  <p className="mt-4 text-xs leading-5 text-muted">{connector.description}</p>
                  <div className="mt-4 flex items-center justify-between border-t border-line pt-3 font-mono text-[10px] text-dim">
                    <span>OAS {connector.openapi_version}</span>
                    <span>{connector.enabled_operation_count}/{connector.operation_count} ON</span>
                  </div>
                </button>
              );
            })}
          </div>
        </aside>

        <section>
          <Panel>
            <PanelHeader className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <PanelTitle>{selected?.name ?? "Operações"}</PanelTitle>
                <p className="mt-2 text-xs text-muted">Campos de contexto: {selected?.context_fields?.join(", ") || "nenhum"}</p>
              </div>
              <div className="flex gap-1" aria-label="Filtrar operações">
                {(["all", "read", "write"] as const).map((value) => (
                  <Button key={value} size="sm" variant={filter === value ? "default" : "ghost"} onClick={() => setFilter(value)}>
                    {value === "all" ? "Todas" : value === "read" ? "Leituras" : "Escritas"}
                  </Button>
                ))}
              </div>
            </PanelHeader>
            {operations.isLoading ? (
              <div className="p-5"><LoadingState label="Lendo operações" /></div>
            ) : operations.error ? (
              <div className="p-5"><ErrorState compact message={operations.error.message} retry={() => void operations.refetch()} /></div>
            ) : (
              <div className="divide-y divide-line">
                {visibleOperations.map((operation) => <OperationRow key={operation.operation_id} operation={operation} />)}
                {!visibleOperations.length ? <p className="p-8 text-center text-sm text-muted">Nenhuma operação neste filtro.</p> : null}
              </div>
            )}
          </Panel>
        </section>
      </div>
    </>
  );
}

function OperationRow({ operation }: { operation: OperationSummary }) {
  const write = operation.access === "write";
  return (
    <article className="grid gap-5 px-5 py-5 transition-colors hover:bg-white/[0.015] lg:grid-cols-[minmax(0,1fr)_280px]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={write ? "warning" : "good"}>{operation.method}</Badge>
          <code className="truncate text-xs text-muted">{operation.path}</code>
          {!operation.enabled ? <Badge tone="danger">disabled</Badge> : null}
        </div>
        <h2 className="mt-4 text-base font-semibold">{operation.operation_id}</h2>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-muted">{operation.summary || "Sem resumo no contrato OpenAPI."}</p>
        <div className="mt-4 flex flex-wrap gap-2">
          <Badge tone={riskTone(operation.risk)}>risco {operation.risk}</Badge>
          {operation.idempotent ? <Badge><RotateCcw size={10} /> idempotente</Badge> : null}
          {operation.requires_confirmation ? <Badge><LockKeyhole size={10} /> confirmação</Badge> : null}
          {operation.requires_direct_request ? <Badge><ShieldAlert size={10} /> pedido direto</Badge> : null}
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 border-l border-line pl-5 text-xs">
        <Meta label="Permissão" value={operation.permission ?? "não exigida"} />
        <Meta label="Timeout" value={`${operation.timeout_seconds}s`} />
        <Meta label="Retries" value={String(operation.max_retries)} />
        <Meta label="Justificativa" value={operation.justification_min_length ? `${operation.justification_min_length}+ chars` : "não exigida"} />
        <div className="col-span-2">
          <dt className="font-mono text-[9px] uppercase tracking-wider text-dim">Escopos</dt>
          <dd className="mt-1 flex items-center gap-1 text-muted"><Check size={12} className="text-ok" /> {operation.required_scopes?.join(", ") || "nenhum"}</dd>
        </div>
      </dl>
    </article>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return <div><dt className="font-mono text-[9px] uppercase tracking-wider text-dim">{label}</dt><dd className="mt-1 text-muted">{value}</dd></div>;
}

function riskTone(risk: OperationSummary["risk"]): "good" | "warning" | "danger" {
  if (risk === "low") return "good";
  if (risk === "medium") return "warning";
  return "danger";
}
