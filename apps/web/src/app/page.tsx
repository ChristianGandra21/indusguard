"use client";

import { useQueries } from "@tanstack/react-query";
import { ArrowRight, Braces, Cable, Database, Gauge, ShieldCheck } from "lucide-react";
import Link from "next/link";

import { ErrorState, LoadingState } from "@/components/data-state";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Panel, PanelContent, PanelHeader, PanelTitle } from "@/components/ui/panel";
import { api } from "@/lib/api";

const pipeline = [
  { label: "OpenAPI", detail: "Contrato", icon: Braces },
  { label: "MCP", detail: "Tools tipadas", icon: Cable },
  { label: "Policy", detail: "Decisão", icon: ShieldCheck },
  { label: "Executor", detail: "HTTP seguro", icon: Gauge },
];

export default function HomePage() {
  const results = useQueries({
    queries: [
      { queryKey: ["health"], queryFn: api.health },
      { queryKey: ["ready"], queryFn: api.ready },
      { queryKey: ["version"], queryFn: api.version },
      { queryKey: ["connectors"], queryFn: api.connectors },
    ],
  });
  const [health, ready, version, connectors] = results;
  const loading = results.some((result) => result.isLoading);
  const failed = results.find((result) => result.error);

  if (loading) return <LoadingState label="Consultando o plano de controle" />;
  if (failed) {
    return (
      <ErrorState
        title="Backend fora de alcance"
        message={failed.error instanceof Error ? failed.error.message : "Falha desconhecida."}
        retry={() => void Promise.all(results.map((result) => result.refetch()))}
      />
    );
  }

  const connectorList = connectors.data ?? [];
  const totalOperations = connectorList.reduce((total, item) => total + item.operation_count, 0);
  const enabledOperations = connectorList.reduce(
    (total, item) => total + item.enabled_operation_count,
    0,
  );

  return (
    <>
      <PageHeader
        eyebrow="Visão do sistema / live"
        title="A camada segura entre intenção e ação."
        description="O IndusGuard transforma contratos OpenAPI em tools de agente, mas mantém identidade, política e execução fora do controle do modelo."
        actions={
          <Badge tone={health.data?.status === "healthy" ? "good" : "danger"}>
            <span className="size-1.5 rounded-full bg-current" /> API operacional
          </Badge>
        }
      />

      <section className="grid gap-px border border-line bg-line sm:grid-cols-2 xl:grid-cols-4" aria-label="Indicadores do sistema">
        <Metric label="Conectores" value={ready.data?.connector_count ?? 0} detail="catálogo validado" />
        <Metric label="Operações" value={totalOperations} detail={`${enabledOperations} habilitadas`} />
        <Metric label="Execução" value={version.data?.execution_mode ?? "—"} detail="escritas reais bloqueadas" accent />
        <Metric label="Release" value={`v${version.data?.version ?? "—"}`} detail={version.data?.environment ?? "—"} />
      </section>

      <div className="mt-6 grid gap-6 xl:grid-cols-[1.45fr_0.75fr]">
        <Panel>
          <PanelHeader className="flex items-center justify-between">
            <PanelTitle>Caminho obrigatório de execução</PanelTitle>
            <span className="font-mono text-[10px] text-dim">FIG. 01</span>
          </PanelHeader>
          <PanelContent className="py-8">
            <div className="grid gap-3 md:grid-cols-[1fr_auto_1fr_auto_1fr_auto_1fr] md:items-center">
              {pipeline.map((node, index) => {
                const Icon = node.icon;
                return (
                  <div key={node.label} className="contents">
                    <div className="group border border-line bg-ink/40 p-4 transition-colors hover:border-line-bright">
                      <Icon className="text-signal" size={19} strokeWidth={1.6} />
                      <p className="mt-7 font-mono text-xs font-semibold uppercase tracking-[0.12em]">{node.label}</p>
                      <p className="mt-1 text-xs text-muted">{node.detail}</p>
                    </div>
                    {index < pipeline.length - 1 ? (
                      <ArrowRight className="mx-auto rotate-90 text-dim md:rotate-0" size={15} aria-hidden="true" />
                    ) : null}
                  </div>
                );
              })}
            </div>
            <div className="mt-7 border-l-2 border-signal bg-signal/[0.05] px-4 py-3">
              <p className="text-sm leading-6 text-muted">
                O LLM propõe. O código valida. A policy decide. Este dashboard apenas observa os metadados produzidos por esse fluxo.
              </p>
            </div>
          </PanelContent>
        </Panel>

        <Panel>
          <PanelHeader className="flex items-center justify-between">
            <PanelTitle>Conectores carregados</PanelTitle>
            <Database size={15} className="text-dim" />
          </PanelHeader>
          <div className="divide-y divide-line">
            {connectorList.length ? (
              connectorList.map((connector) => (
                <div key={connector.id} className="px-5 py-4">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-semibold">{connector.name}</p>
                      <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-dim">OpenAPI {connector.openapi_version}</p>
                    </div>
                    <Badge tone="neutral">{connector.enabled_operation_count}/{connector.operation_count}</Badge>
                  </div>
                </div>
              ))
            ) : (
              <p className="px-5 py-6 text-sm leading-6 text-muted">Nenhum conector carregado neste ambiente.</p>
            )}
          </div>
          <Link href="/connectors" className="flex items-center justify-between border-t border-line px-5 py-4 text-sm text-muted hover:bg-white/[0.02] hover:text-foreground">
            Inspecionar catálogo <ArrowRight size={15} />
          </Link>
        </Panel>
      </div>

      <section className="mt-6 grid gap-4 border border-line bg-panel/50 p-5 md:grid-cols-[auto_1fr_auto] md:items-center">
        <ShieldCheck className="text-ok" size={22} />
        <div>
          <h2 className="text-sm font-semibold">Projeção pública mínima</h2>
          <p className="mt-1 text-xs leading-5 text-muted">Mensagens, respostas, argumentos, payloads e contexto confiável não são carregados pelas consultas deste painel.</p>
        </div>
        <Badge tone="good">read only</Badge>
      </section>
    </>
  );
}

function Metric({ label, value, detail, accent = false }: { label: string; value: string | number; detail: string; accent?: boolean }) {
  return (
    <div className="bg-panel p-5">
      <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-dim">{label}</p>
      <p className={`metric-value mt-3 text-3xl font-semibold ${accent ? "text-signal" : "text-foreground"}`}>{value}</p>
      <p className="mt-2 text-xs text-muted">{detail}</p>
    </div>
  );
}
