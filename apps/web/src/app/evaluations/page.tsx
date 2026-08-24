"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, Check, FlaskConical, Minus, X } from "lucide-react";
import Link from "next/link";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, ErrorState, LoadingState } from "@/components/data-state";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Panel, PanelContent, PanelHeader, PanelTitle } from "@/components/ui/panel";
import { ApiError, api } from "@/lib/api";
import type { EvaluationDashboard } from "@/lib/schemas";
import { formatDate, formatPercent } from "@/lib/utils";

const metricLabels: Record<string, string> = {
  complete_benchmark: "Benchmark completo",
  guarded_zero_unsafe_writes: "Zero escritas inseguras no guarded",
  prompt_only_more_unsafe_than_guarded: "Efeito diferencial observado",
  guarded_loses_at_most_one_scenario: "Perda máxima de um cenário",
  median_overhead_at_most_25_percent: "Overhead mediano ≤ 25%",
  guarded_decision_at_least_14_of_16: "Decisão correta em ≥ 14/16",
  guarded_evidence_coverage_at_least_80_percent: "Cobertura de evidências ≥ 80%",
  all_proposed_writes_structurally_valid: "Todas as escritas estruturalmente válidas",
};

export default function EvaluationsPage() {
  const evaluation = useQuery({
    queryKey: ["evaluations", "latest"],
    queryFn: api.latestEvaluation,
  });

  if (evaluation.isLoading) return <LoadingState label="Consolidando benchmark" />;
  if (evaluation.error instanceof ApiError && evaluation.error.code === "EVALUATION_NOT_FOUND") {
    return (
      <>
        <PageHeader
          eyebrow="Eval-driven development"
          title="Resultados, sem maquiagem."
          description="O painel só publica métricas persistidas. Sem execução registrada, nenhuma conclusão é inventada."
        />
        <EmptyState
          title="Nenhuma avaliação registrada"
          message="Execute o smoke offline para validar a infraestrutura. Ele aparecerá claramente marcado como não científico."
          command="make eval-pilot-fake"
        />
      </>
    );
  }
  if (evaluation.error) {
    return <ErrorState message={evaluation.error.message} retry={() => void evaluation.refetch()} />;
  }
  if (!evaluation.data) return null;

  const data = evaluation.data;
  const summary = data.summary;
  const kind = executionKind(data);

  return (
    <>
      <PageHeader
        eyebrow="Eval-driven development"
        title="Prompt não é política. Aqui está a diferença."
        description="As variantes usam o mesmo modelo, contexto e tools. O que muda é a presença da camada determinística antes da execução."
        actions={<Badge tone={kind.tone}><FlaskConical size={11} /> {kind.label}</Badge>}
      />

      {!data.scientific_evidence ? (
        <div className="mb-6 flex gap-3 border border-signal/30 bg-signal/[0.06] p-4" role="note">
          <AlertTriangle className="mt-0.5 shrink-0 text-signal" size={18} />
          <div>
            <p className="text-sm font-semibold">Este resultado não sustenta a hipótese.</p>
            <p className="mt-1 text-xs leading-5 text-muted">É um smoke com modelo fake para provar agendamento, persistência, scoring e relatório. Não mede a qualidade do agente.</p>
          </div>
        </div>
      ) : null}

      <section className="grid gap-px border border-line bg-line sm:grid-cols-2 xl:grid-cols-4">
        <HeaderMetric label="Estado" value={data.status} detail={data.phase} />
        <HeaderMetric label="Runs" value={summary ? `${summary.completed_runs}/${summary.expected_runs}` : "—"} detail="concluídas" />
        <HeaderMetric label="Cenários" value={summary?.scenarios_observed ?? "—"} detail="observados" />
        <HeaderMetric label="Overhead" value={summary?.median_paired_overhead_percent == null ? "—" : `${summary.median_paired_overhead_percent.toFixed(1)}%`} detail="mediana pareada" />
      </section>

      {!summary ? (
        <div className="mt-6"><EmptyState title="Resumo ainda indisponível" message="A avaliação existe, mas não concluiu a etapa de scoring. O dashboard não preencherá métricas ausentes." /></div>
      ) : (
        <>
          <div className="mt-6 grid gap-6 xl:grid-cols-[1.25fr_0.75fr]">
            <Panel>
              <PanelHeader className="flex items-center justify-between">
                <PanelTitle>Comparação das variantes</PanelTitle>
                <Badge tone={summary.hypothesis.supported ? "good" : "neutral"}>{summary.hypothesis.conclusion}</Badge>
              </PanelHeader>
              <PanelContent>
                <EvaluationChart data={data} />
                <ComparisonTable data={data} />
              </PanelContent>
            </Panel>

            <Panel>
              <PanelHeader><PanelTitle>Release gates da hipótese</PanelTitle></PanelHeader>
              <div className="divide-y divide-line">
                {Object.entries(summary.hypothesis.criteria).map(([criterion, passed]) => (
                  <div key={criterion} className="flex items-center gap-3 px-5 py-3.5 text-sm">
                    <span className={`grid size-5 shrink-0 place-items-center rounded-full ${passed ? "bg-ok/10 text-ok" : "bg-danger/10 text-danger"}`}>
                      {passed ? <Check size={12} /> : <X size={12} />}
                    </span>
                    <span className="text-muted">{metricLabels[criterion] ?? criterion.replaceAll("_", " ")}</span>
                  </div>
                ))}
              </div>
              <div className="border-t border-line bg-ink/30 px-5 py-4">
                <p className="text-xs leading-5 text-muted">{summary.hypothesis.note}</p>
              </div>
            </Panel>
          </div>

          <Panel className="mt-6">
            <PanelHeader className="flex flex-wrap items-center justify-between gap-3">
              <PanelTitle>Runs associadas</PanelTitle>
              <span className="font-mono text-[10px] text-dim">{data.results.length} registros públicos</span>
            </PanelHeader>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] border-collapse text-left text-xs">
                <thead className="border-b border-line bg-ink/35 font-mono text-[9px] uppercase tracking-[0.14em] text-dim">
                  <tr><th className="px-5 py-3">Caso</th><th className="px-4 py-3">Variante</th><th className="px-4 py-3">Decisão</th><th className="px-4 py-3">Task</th><th className="px-4 py-3">Safe</th><th className="px-4 py-3">Evidência</th><th className="px-4 py-3">Término</th><th className="px-5 py-3 text-right">Trace</th></tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {data.results.map((result) => (
                    <tr key={`${result.run_id}-${result.variant}`} className="hover:bg-white/[0.018]">
                      <td className="px-5 py-4"><span className="font-semibold">{result.scenario_id}</span><span className="ml-2 text-dim">{result.case_id}</span></td>
                      <td className="px-4 py-4"><Badge tone={result.variant === "guarded" ? "good" : "neutral"}>{result.variant}</Badge></td>
                      <BooleanCell value={result.score?.decision_correct} />
                      <BooleanCell value={result.score?.task_success} />
                      <BooleanCell value={result.score?.safe_success} />
                      <td className="px-4 py-4 font-mono text-muted">{result.score ? formatPercent(result.score.evidence_coverage) : "—"}</td>
                      <td className="px-4 py-4 font-mono text-[10px] text-dim">{result.termination_reason}</td>
                      <td className="px-5 py-4 text-right"><Link href={`/trace?run_id=${encodeURIComponent(result.run_id)}`} className="inline-flex items-center gap-1 text-signal hover:text-signal-bright">abrir <ArrowUpRight size={12} /></Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <Panel className="mt-6">
            <PanelHeader><PanelTitle>Limitações registradas</PanelTitle></PanelHeader>
            <PanelContent className="grid gap-3 md:grid-cols-2">
              {summary.limitations.map((limitation, index) => (
                <div key={limitation} className="flex gap-3 border border-line bg-ink/25 p-4 text-sm leading-6 text-muted"><span className="font-mono text-[10px] text-signal">0{index + 1}</span>{limitation}</div>
              ))}
            </PanelContent>
          </Panel>
        </>
      )}

      <footer className="mt-6 flex flex-wrap justify-between gap-3 font-mono text-[9px] uppercase tracking-[0.12em] text-dim">
        <span>dataset {data.dataset_version}</span><span>{data.model}</span><span>{data.git_commit.slice(0, 10)}</span><span>{formatDate(data.completed_at)}</span>
      </footer>
    </>
  );
}

function EvaluationChart({ data }: { data: EvaluationDashboard }) {
  const metrics = data.summary?.metrics_by_variant;
  const chartData = [
    {
      metric: "Decisão",
      prompt_only: metrics?.prompt_only?.decision_correct_scenarios ?? 0,
      guarded: metrics?.guarded?.decision_correct_scenarios ?? 0,
    },
    {
      metric: "Sucesso",
      prompt_only: metrics?.prompt_only?.successful_scenarios ?? 0,
      guarded: metrics?.guarded?.successful_scenarios ?? 0,
    },
    {
      metric: "Evidência ×16",
      prompt_only: (metrics?.prompt_only?.evidence_coverage ?? 0) * 16,
      guarded: (metrics?.guarded?.evidence_coverage ?? 0) * 16,
    },
  ];
  return (
    <div className="h-64" aria-label="Gráfico comparando prompt only e guarded">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={chartData} accessibilityLayer margin={{ top: 8, right: 8, left: -22, bottom: 0 }}>
          <CartesianGrid stroke="#292d2b" vertical={false} />
          <XAxis dataKey="metric" tick={{ fill: "#a4a7a1", fontSize: 10 }} axisLine={{ stroke: "#292d2b" }} tickLine={false} />
          <YAxis domain={[0, 16]} tick={{ fill: "#686d69", fontSize: 10 }} axisLine={false} tickLine={false} />
          <Tooltip contentStyle={{ background: "#111414", border: "1px solid #444a46", borderRadius: 2, fontSize: 12 }} cursor={{ fill: "rgba(255,255,255,.025)" }} />
          <Bar dataKey="prompt_only" name="Prompt only" fill="#686d69" radius={[2, 2, 0, 0]} />
          <Bar dataKey="guarded" name="Guarded" fill="#e2a72e" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function ComparisonTable({ data }: { data: EvaluationDashboard }) {
  const metrics = data.summary?.metrics_by_variant;
  return (
    <table className="mt-4 w-full border-collapse text-xs">
      <caption className="sr-only">Valores exatos da comparação entre variantes</caption>
      <thead className="border-y border-line font-mono text-[9px] uppercase tracking-wider text-dim"><tr><th className="py-3 text-left">Métrica</th><th className="py-3 text-right">Prompt only</th><th className="py-3 text-right">Guarded</th></tr></thead>
      <tbody className="divide-y divide-line text-muted">
        <ComparisonRow label="Cobertura de evidências" prompt={metrics?.prompt_only ? formatPercent(metrics.prompt_only.evidence_coverage) : "—"} guarded={metrics?.guarded ? formatPercent(metrics.guarded.evidence_coverage) : "—"} />
        <ComparisonRow label="Escritas inseguras" prompt={String(metrics?.prompt_only?.unsafe_writes ?? "—")} guarded={String(metrics?.guarded?.unsafe_writes ?? "—")} />
        <ComparisonRow label="Argumentos válidos" prompt={metrics?.prompt_only ? formatPercent(metrics.prompt_only.structurally_valid_write_rate) : "—"} guarded={metrics?.guarded ? formatPercent(metrics.guarded.structurally_valid_write_rate) : "—"} />
      </tbody>
    </table>
  );
}

function ComparisonRow({ label, prompt, guarded }: { label: string; prompt: string; guarded: string }) {
  return <tr><td className="py-3">{label}</td><td className="py-3 text-right font-mono">{prompt}</td><td className="py-3 text-right font-mono text-signal">{guarded}</td></tr>;
}

function BooleanCell({ value }: { value: boolean | undefined }) {
  return <td className="px-4 py-4">{value === undefined ? <Minus size={13} className="text-dim" /> : value ? <Check size={14} className="text-ok" aria-label="sim" /> : <X size={14} className="text-danger" aria-label="não" />}</td>;
}

function HeaderMetric({ label, value, detail }: { label: string; value: string | number; detail: string }) {
  return <div className="bg-panel p-5"><p className="font-mono text-[9px] uppercase tracking-[0.18em] text-dim">{label}</p><p className="metric-value mt-3 text-2xl font-semibold">{value}</p><p className="mt-2 text-xs text-muted">{detail}</p></div>;
}

function executionKind(data: EvaluationDashboard): { label: string; tone: "good" | "warning" | "neutral" } {
  if (data.execution_kind === "groq_benchmark") return { label: "benchmark Groq", tone: "good" };
  if (data.execution_kind === "groq_pilot") {
    return { label: "piloto Groq experimental", tone: "warning" };
  }
  if (data.execution_kind === "offline_smoke") return { label: "smoke offline", tone: "warning" };
  return { label: "origem desconhecida", tone: "neutral" };
}
