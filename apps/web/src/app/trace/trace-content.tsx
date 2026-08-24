"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Braces, Clock3, EyeOff, Search } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useState } from "react";

import { EmptyState, ErrorState, LoadingState } from "@/components/data-state";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelContent, PanelHeader, PanelTitle } from "@/components/ui/panel";
import { ApiError, api } from "@/lib/api";
import type { RunTrace } from "@/lib/schemas";
import { formatDate, formatDuration } from "@/lib/utils";

export function TraceContent() {
  const params = useSearchParams();
  const router = useRouter();
  const runId = params.get("run_id")?.trim() ?? "";
  const [input, setInput] = useState(runId);

  const trace = useQuery({
    queryKey: ["trace", runId],
    queryFn: () => api.trace(runId),
    enabled: Boolean(runId),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = input.trim();
    router.push(normalized ? `/trace?run_id=${encodeURIComponent(normalized)}` : "/trace");
  }

  return (
    <>
      <PageHeader
        eyebrow="Timeline / somente metadados"
        title="Veja o caminho. Não o conteúdo."
        description="O trace público explica quais ferramentas foram acionadas e quais políticas decidiram o fluxo, sem carregar a solicitação, a resposta ou dados da API."
        actions={<Badge tone="good"><EyeOff size={11} /> conteúdo omitido</Badge>}
      />

      <form onSubmit={submit} className="mb-6 flex flex-col gap-2 sm:flex-row" role="search">
        <label htmlFor="run-id" className="sr-only">ID da run</label>
        <div className="flex min-w-0 flex-1 items-center gap-3 border border-line bg-panel px-4 focus-within:border-signal/60">
          <Search size={16} className="shrink-0 text-dim" />
          <input id="run-id" value={input} onChange={(event) => setInput(event.target.value)} placeholder="Cole um run_id, por exemplo 1b94…" className="h-11 min-w-0 flex-1 bg-transparent font-mono text-xs text-foreground outline-none placeholder:text-dim" />
        </div>
        <Button type="submit">Carregar trace <ArrowRight size={14} /></Button>
      </form>

      {!runId ? (
        <EmptyState title="Informe uma run" message="Abra uma run pela página de avaliações ou cole um UUID conhecido. Não existe listagem pública de conversas." />
      ) : trace.isLoading ? (
        <LoadingState label="Consultando timeline segura" />
      ) : trace.error instanceof ApiError && trace.error.code === "TRACE_NOT_FOUND" ? (
        <EmptyState title="Trace não encontrado" message="O identificador não corresponde a uma run persistida neste ambiente." />
      ) : trace.error ? (
        <ErrorState message={trace.error.message} retry={() => void trace.refetch()} />
      ) : trace.data ? (
        <TraceView trace={trace.data} />
      ) : null}
    </>
  );
}

function TraceView({ trace }: { trace: RunTrace }) {
  const policyBySequence = new Map(trace.policy_decisions.map((item) => [item.tool_sequence, item]));
  const evidenceById = new Map(trace.evidence.map((item) => [item.evidence_id, item]));

  return (
    <>
      <section className="grid gap-px border border-line bg-line sm:grid-cols-2 xl:grid-cols-5">
        <TraceMetric label="Estado" value={trace.status} detail={trace.termination_reason} />
        <TraceMetric label="Decisão" value={trace.decision} detail={trace.intent_id ?? "intenção ambígua"} />
        <TraceMetric label="Tools" value={trace.tool_call_count} detail={`${trace.evidence.length} evidências`} />
        <TraceMetric label="Tokens" value={trace.total_tokens} detail={`${trace.model_calls} chamadas de modelo`} />
        <TraceMetric label="Latência" value={formatDuration(trace.latency_ms)} detail={`${trace.truncations} truncamentos`} />
      </section>

      <div className="mt-6 grid gap-6 xl:grid-cols-[1fr_330px]">
        <Panel>
          <PanelHeader className="flex items-center justify-between"><PanelTitle>Sequência operacional</PanelTitle><Badge tone={trace.observability_degraded ? "warning" : "good"}>{trace.observability_degraded ? "telemetria degradada" : "observado"}</Badge></PanelHeader>
          {trace.tool_calls.length ? (
            <ol className="divide-y divide-line">
              {trace.tool_calls.map((call) => {
                const policy = policyBySequence.get(call.sequence);
                const evidence = call.evidence_id ? evidenceById.get(call.evidence_id) : undefined;
                return (
                  <li key={call.sequence} className="relative grid gap-4 px-5 py-5 md:grid-cols-[42px_minmax(0,1fr)]">
                    <span className="grid size-9 place-items-center border border-line bg-ink font-mono text-[10px] text-signal">{String(call.sequence).padStart(2, "0")}</span>
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div><p className="font-mono text-xs font-semibold">{call.tool_alias}</p><p className="mt-1 truncate font-mono text-[10px] text-dim">{call.mcp_tool_name ?? "tool MCP indisponível"}</p></div>
                        <div className="flex gap-2"><Badge tone={outcomeTone(call.outcome)}>{call.outcome}</Badge><Badge><Clock3 size={10} /> {formatDuration(call.latency_ms)}</Badge></div>
                      </div>
                      <div className="mt-4 grid gap-3 md:grid-cols-2">
                        <div className="border border-line bg-ink/25 p-3"><p className="font-mono text-[9px] uppercase tracking-wider text-dim">Policy</p><p className="mt-2 text-xs text-muted">{policy ? `${policy.outcome} · ${policy.operation_id}` : "Sem decisão registrada"}</p>{policy?.reason_codes.length ? <div className="mt-2 flex flex-wrap gap-1">{policy.reason_codes.map((code) => <Badge key={code} tone="neutral">{code}</Badge>)}</div> : null}</div>
                        <div className="border border-line bg-ink/25 p-3"><p className="font-mono text-[9px] uppercase tracking-wider text-dim">Evidência</p><p className="mt-2 text-xs text-muted">{evidence ? `${evidence.evidence_id} · HTTP ${evidence.status_code ?? "—"}` : "Nenhuma evidência vinculada"}</p>{evidence ? <p className="mt-2 font-mono text-[9px] text-dim">{evidence.stored_size_bytes}/{evidence.original_size_bytes} bytes {evidence.truncated ? "· truncado" : "· integral"}</p> : null}</div>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          ) : <PanelContent><EmptyState title="Nenhuma tool chamada" message="A run terminou sem atravessar operações MCP." /></PanelContent>}
        </Panel>

        <div className="space-y-6">
          <Panel>
            <PanelHeader><PanelTitle>Versões correlacionadas</PanelTitle></PanelHeader>
            <PanelContent><dl className="space-y-4"><Version label="Modelo" value={trace.model} /><Version label="Prompt" value={trace.prompt_version} /><Version label="Domínio" value={trace.domain_version} /><Version label="Policy" value={trace.policy_version} /><Version label="Seed" value={String(trace.seed)} /></dl></PanelContent>
          </Panel>
          <Panel>
            <PanelHeader><PanelTitle>Janela temporal</PanelTitle></PanelHeader>
            <PanelContent className="space-y-4"><div><p className="font-mono text-[9px] uppercase tracking-wider text-dim">Início</p><p className="mt-1 text-xs text-muted">{formatDate(trace.started_at)}</p></div><div><p className="font-mono text-[9px] uppercase tracking-wider text-dim">Fim</p><p className="mt-1 text-xs text-muted">{formatDate(trace.completed_at)}</p></div></PanelContent>
          </Panel>
          <div className="border border-info/25 bg-info/[0.05] p-4"><div className="flex gap-3"><Braces size={17} className="mt-0.5 shrink-0 text-info" /><p className="text-xs leading-5 text-muted">Os nomes e outcomes são públicos. Argumentos e resultados não fazem parte deste contrato.</p></div></div>
        </div>
      </div>
    </>
  );
}

function TraceMetric({ label, value, detail }: { label: string; value: string | number; detail: string }) {
  return <div className="bg-panel p-5"><p className="font-mono text-[9px] uppercase tracking-[0.16em] text-dim">{label}</p><p className="metric-value mt-3 truncate text-xl font-semibold">{value}</p><p className="mt-2 truncate text-xs text-muted">{detail}</p></div>;
}

function Version({ label, value }: { label: string; value: string }) {
  return <div className="flex items-start justify-between gap-3"><dt className="font-mono text-[9px] uppercase tracking-wider text-dim">{label}</dt><dd className="max-w-[190px] break-all text-right font-mono text-[10px] text-muted">{value}</dd></div>;
}

function outcomeTone(outcome: string): "good" | "warning" | "danger" | "neutral" {
  if (["success", "executed", "allow"].includes(outcome)) return "good";
  if (["simulated", "require_confirmation"].includes(outcome)) return "warning";
  if (["blocked", "failed"].includes(outcome)) return "danger";
  return "neutral";
}
