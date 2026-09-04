"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  Braces,
  ClipboardCheck,
  CircleStop,
  Clock3,
  FileSearch,
  KeyRound,
  LockKeyhole,
  LogOut,
  Route,
  ShieldCheck,
  TerminalSquare,
} from "lucide-react";
import Link from "next/link";
import { FormEvent, useState, useSyncExternalStore } from "react";

import { ErrorState, LoadingState } from "@/components/data-state";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelContent, PanelHeader, PanelTitle } from "@/components/ui/panel";
import { ApiError, api } from "@/lib/api";
import type { PublicRunResult } from "@/lib/schemas";

const TOKEN_STORAGE_KEY = "indusguard.owner_token";
const TOKEN_CHANGE_EVENT = "indusguard-owner-token-change";
const SERVER_OWNED_CONTEXT_FIELDS = new Set(["user_id"]);

type RunPreset = {
  id: string;
  connectorId: string;
  label: string;
  message: string;
  context: Record<string, string>;
  directRequest: boolean;
};

type ContextOption = {
  value: string;
  label: string;
};

const runPresets: RunPreset[] = [
  {
    id: "synthetic-status",
    connectorId: "synthetic",
    label: "Widget status",
    message: "Qual é o estado atual do widget widget-1?",
    context: { widget_id: "widget-1" },
    directRequest: false,
  },
  {
    id: "tractian-investigation",
    connectorId: "tractian",
    label: "Falha sem aviso",
    message: "O redutor da correia transportadora quebrou ontem e eu não recebi nenhum aviso. Por quê?",
    context: {
      company_id: "comp_mineracao_andes",
      asset_id: "asset_G501",
      case_id: "case_tkt_inv_04",
    },
    directRequest: false,
  },
  {
    id: "tractian-specialist",
    connectorId: "tractian",
    label: "Especialista Tractian",
    message: "Esse compressor tá com comportamento estranho e o insight não convence. Quero que um especialista da Tractian veja.",
    context: {
      company_id: "comp_petro_delta",
      asset_id: "asset_C710",
      case_id: "case_tkt_exe_13",
    },
    directRequest: true,
  },
];

const contextFieldOptions: Record<string, ContextOption[]> = {
  asset_id: [
    { value: "asset_G501", label: "asset_G501 · Redutor da correia transportadora" },
    { value: "asset_C710", label: "asset_C710 · Compressor de gás" },
  ],
  case_id: [
    { value: "case_tkt_inv_04", label: "case_tkt_inv_04 · Falha sem aviso" },
    { value: "case_tkt_exe_13", label: "case_tkt_exe_13 · Especialista Tractian" },
  ],
  company_id: [
    { value: "comp_mineracao_andes", label: "comp_mineracao_andes · Mineração Andes" },
    { value: "comp_petro_delta", label: "comp_petro_delta · Petro Delta" },
  ],
  widget_id: [{ value: "widget-1", label: "widget-1 · Widget ativo" }],
};

const linkedContextByValue: Record<string, Record<string, string>> = {
  asset_C710: { company_id: "comp_petro_delta" },
  asset_G501: { company_id: "comp_mineracao_andes" },
  case_tkt_exe_13: {
    company_id: "comp_petro_delta",
    asset_id: "asset_C710",
  },
  case_tkt_inv_04: {
    company_id: "comp_mineracao_andes",
    asset_id: "asset_G501",
  },
};

function subscribeToken(onChange: () => void) {
  window.addEventListener(TOKEN_CHANGE_EVENT, onChange);
  return () => window.removeEventListener(TOKEN_CHANGE_EVENT, onChange);
}

function tokenSnapshot() {
  return window.sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
}

function updateStoredToken(value: string | null) {
  if (value === null) window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  else window.sessionStorage.setItem(TOKEN_STORAGE_KEY, value);
  window.dispatchEvent(new Event(TOKEN_CHANGE_EVENT));
}

export default function PlaygroundPage() {
  const config = useQuery({
    queryKey: ["playground", "config"],
    queryFn: api.playgroundConfig,
  });
  const token = useSyncExternalStore(subscribeToken, tokenSnapshot, () => "");
  const [tokenDraft, setTokenDraft] = useState("");
  const [connectorId, setConnectorId] = useState("");
  const [contextDraft, setContextDraft] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [directRequest, setDirectRequest] = useState(false);

  const selectedConnectorId = connectorId || config.data?.connectors[0]?.id || "synthetic";
  const selectedConnector = config.data?.connectors.find(
    (connector) => connector.id === selectedConnectorId,
  );
  const editableContextFields =
    selectedConnector?.context_fields.filter((field) => !SERVER_OWNED_CONTEXT_FIELDS.has(field)) ?? [];
  const contextPayload = Object.fromEntries(
    Object.entries(contextDraft).filter(
      ([key, value]) => !SERVER_OWNED_CONTEXT_FIELDS.has(key) && value.trim(),
    ),
  );
  const contextComplete = editableContextFields.every((field) => contextDraft[field]?.trim());

  const run = useMutation({
    mutationFn: () =>
      api.run(
        {
          connector_id: selectedConnectorId,
          message,
          seed: 42,
          context: contextPayload,
          direct_request: directRequest,
        },
        token,
      ),
  });

  function changeConnector(value: string) {
    setConnectorId(value);
    setContextDraft({});
    run.reset();
  }

  function updateContextField(field: string, value: string) {
    setContextDraft((current) => ({
      ...current,
      [field]: value,
      ...(linkedContextByValue[value] ?? {}),
    }));
  }

  function applyPreset(preset: RunPreset) {
    setConnectorId(preset.connectorId);
    setContextDraft(preset.context);
    setMessage(preset.message);
    setDirectRequest(preset.directRequest);
    run.reset();
  }

  function saveToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = tokenDraft.trim();
    if (!normalized) return;
    updateStoredToken(normalized);
    setTokenDraft("");
    run.reset();
  }

  function logout() {
    updateStoredToken(null);
    setTokenDraft("");
    run.reset();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!message.trim() || !contextComplete) return;
    run.mutate();
  }

  if (config.isLoading) return <LoadingState label="Acordando o plano de controle" />;
  if (config.error) {
    const coldStart = config.error instanceof ApiError && config.error.code === "API_UNREACHABLE";
    return (
      <ErrorState
        title={coldStart ? "Backend em cold start" : "Configuração indisponível"}
        message={
          coldStart
            ? "O serviço gratuito pode levar cerca de um minuto para acordar. Aguarde e tente novamente."
            : config.error.message
        }
        retry={() => void config.refetch()}
      />
    );
  }
  if (!config.data) return null;

  return (
    <>
      <PageHeader
        eyebrow="Owner-only agent lab"
        title="Teste o agente. Preserve a fronteira."
        description="Uma run stateless atravessa Groq, MCP e policy. O token fica somente nesta aba e toda escrita permanece simulada."
        actions={
          <div className="flex flex-wrap gap-2">
            <Badge tone="warning">simulate only</Badge>
            <Badge tone={config.data.model_configured ? "good" : "danger"}>
              {config.data.model_configured ? "modelo pronto" : "modelo ausente"}
            </Badge>
          </div>
        }
      />

      {!config.data.enabled ? (
        <ErrorState
          title="Playground desabilitado"
          message="O backend está em modo somente leitura. Ative as runs públicas apenas no ambiente do proprietário."
        />
      ) : !config.data.model_configured ? (
        <ErrorState
          title="Modelo ainda não configurado"
          message="Adicione GROQ_API_KEY somente ao ambiente do backend. Ela nunca deve entrar no frontend."
        />
      ) : !token ? (
        <AccessPanel
          tokenDraft={tokenDraft}
          setTokenDraft={setTokenDraft}
          saveToken={saveToken}
        />
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,0.82fr)_minmax(0,1.18fr)]">
          <div className="space-y-6">
            <SessionStrip logout={logout} />
            <RunForm
              connectorId={selectedConnectorId}
              setConnectorId={changeConnector}
              connectors={config.data.connectors}
              contextDraft={contextDraft}
              editableContextFields={editableContextFields}
              updateContextField={updateContextField}
              presets={runPresets.filter((preset) =>
                config.data.connectors.some((connector) => connector.id === preset.connectorId),
              )}
              applyPreset={applyPreset}
              message={message}
              setMessage={setMessage}
              maxMessageLength={config.data.max_message_length}
              directRequest={directRequest}
              setDirectRequest={setDirectRequest}
              submit={submit}
              pending={run.isPending}
              canSubmit={Boolean(message.trim() && contextComplete)}
            />
            <LimitsPanel
              rate={config.data.rate_limit_per_hour}
              concurrency={config.data.concurrency_limit}
            />
          </div>

          <div aria-live="polite">
            {run.isPending ? (
              <LoadingState label="Executando grafo protegido" />
            ) : run.error ? (
              <RunError error={run.error} retry={() => run.mutate()} />
            ) : run.data ? (
              <RunResult result={run.data} />
            ) : (
              <WaitingPanel />
            )}
          </div>
        </div>
      )}
    </>
  );
}

function AccessPanel({
  tokenDraft,
  setTokenDraft,
  saveToken,
}: {
  tokenDraft: string;
  setTokenDraft: (value: string) => void;
  saveToken: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <Panel className="mx-auto max-w-2xl">
      <PanelHeader className="flex items-center gap-3">
        <span className="grid size-10 place-items-center border border-signal/40 bg-signal/10 text-signal">
          <LockKeyhole size={18} />
        </span>
        <div>
          <PanelTitle>Acesso do proprietário</PanelTitle>
          <p className="mt-1 text-xs text-muted">O token desaparece ao fechar esta aba.</p>
        </div>
      </PanelHeader>
      <PanelContent>
        <form onSubmit={saveToken} className="space-y-4">
          <label className="block">
            <span className="field-label">Token do proprietário</span>
            <span className="relative mt-2 block">
              <KeyRound
                className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-dim"
                size={16}
              />
              <input
                type="password"
                value={tokenDraft}
                onChange={(event) => setTokenDraft(event.target.value)}
                autoComplete="off"
                className="field-control pl-10"
                placeholder="Bearer configurado no backend"
              />
            </span>
          </label>
          <div className="flex flex-wrap items-center justify-between gap-4 border-t border-line pt-4">
            <p className="max-w-md text-xs leading-5 text-muted">
              Armazenado em <code className="text-signal">sessionStorage</code>. Nunca em URL,
              cookie, banco ou variável <code className="text-signal">NEXT_PUBLIC</code>.
            </p>
            <Button type="submit" disabled={!tokenDraft.trim()}>
              Salvar acesso nesta sessão <ArrowRight size={15} />
            </Button>
          </div>
        </form>
      </PanelContent>
    </Panel>
  );
}

function SessionStrip({ logout }: { logout: () => void }) {
  return (
    <div className="flex items-center justify-between gap-4 border border-ok/25 bg-ok/[0.05] px-4 py-3">
      <div className="flex items-center gap-3">
        <ShieldCheck size={17} className="text-ok" />
        <div>
          <p className="text-sm font-semibold">Sessão local ativa</p>
          <p className="text-[11px] text-muted">Token oculto e não persistido no resultado</p>
        </div>
      </div>
      <Button variant="ghost" size="sm" onClick={logout} aria-label="Encerrar sessão">
        <LogOut size={14} /> Sair
      </Button>
    </div>
  );
}

function RunForm({
  connectorId,
  setConnectorId,
  connectors,
  contextDraft,
  editableContextFields,
  updateContextField,
  presets,
  applyPreset,
  message,
  setMessage,
  maxMessageLength,
  directRequest,
  setDirectRequest,
  submit,
  pending,
  canSubmit,
}: {
  connectorId: string;
  setConnectorId: (value: string) => void;
  connectors: { id: string; name: string; context_fields: string[] }[];
  contextDraft: Record<string, string>;
  editableContextFields: string[];
  updateContextField: (field: string, value: string) => void;
  presets: RunPreset[];
  applyPreset: (preset: RunPreset) => void;
  message: string;
  setMessage: (value: string) => void;
  maxMessageLength: number;
  directRequest: boolean;
  setDirectRequest: (value: boolean) => void;
  submit: (event: FormEvent<HTMLFormElement>) => void;
  pending: boolean;
  canSubmit: boolean;
}) {
  return (
    <Panel>
      <PanelHeader>
        <PanelTitle>Nova run stateless</PanelTitle>
      </PanelHeader>
      <PanelContent>
        <form onSubmit={submit} className="space-y-5">
          {presets.length ? (
            <div>
              <span className="field-label">Roteiros de demo</span>
              <div className="mt-2 grid gap-2 sm:grid-cols-3">
                {presets.map((preset) => (
                  <Button
                    key={preset.id}
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-auto min-h-11 justify-start px-3 py-2 text-left"
                    onClick={() => applyPreset(preset)}
                  >
                    <ClipboardCheck size={14} /> {preset.label}
                  </Button>
                ))}
              </div>
            </div>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <label>
              <span className="field-label">Conector público</span>
              <select
                value={connectorId}
                onChange={(event) => setConnectorId(event.target.value)}
                className="field-control mt-2"
              >
                {connectors.map((connector) => (
                  <option key={connector.id} value={connector.id}>
                    {connector.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {editableContextFields.length ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {editableContextFields.map((field) => (
                <label key={field}>
                  <span className="field-label">{field}</span>
                  {contextFieldOptions[field]?.length ? (
                    <select
                      value={contextDraft[field] ?? ""}
                      onChange={(event) => updateContextField(field, event.target.value)}
                      className="field-control mt-2 font-mono text-sm"
                      required
                    >
                      <option value="">Selecione {field}</option>
                      {contextFieldOptions[field].map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={contextDraft[field] ?? ""}
                      onChange={(event) => updateContextField(field, event.target.value)}
                      className="field-control mt-2 font-mono text-sm"
                      placeholder={contextPlaceholder(field)}
                      required
                    />
                  )}
                </label>
              ))}
            </div>
          ) : null}

          <label className="block">
            <span className="flex items-center justify-between gap-3">
              <span className="field-label">Solicitação</span>
              <span className="font-mono text-[9px] text-dim">
                {message.length}/{maxMessageLength}
              </span>
            </span>
            <textarea
              aria-label="Solicitação"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              className="field-control mt-2 min-h-36 resize-y leading-6"
              maxLength={maxMessageLength}
              placeholder="Ex.: Qual é o estado atual do widget widget-1?"
              required
            />
          </label>

          <label className="flex cursor-pointer items-start gap-3 border border-line bg-ink/30 p-3.5">
            <input
              type="checkbox"
              checked={directRequest}
              onChange={(event) => setDirectRequest(event.target.checked)}
              className="mt-0.5 size-4 accent-signal"
            />
            <span>
              <span className="block text-sm font-medium">Este é um pedido direto de ação</span>
              <span className="mt-1 block text-xs leading-5 text-muted">
                Marque apenas quando estiver solicitando explicitamente uma alteração. A policy
                ainda poderá simular ou bloquear.
              </span>
            </span>
          </label>

          <Button
            type="submit"
            className="w-full"
            disabled={pending || !canSubmit}
          >
            {pending ? <Activity className="animate-pulse" size={15} /> : <TerminalSquare size={15} />}
            Executar agente protegido
          </Button>
        </form>
      </PanelContent>
    </Panel>
  );
}

function contextPlaceholder(field: string) {
  const placeholders: Record<string, string> = {
    asset_id: "asset_G501",
    case_id: "case_tkt_inv_04",
    company_id: "comp_mineracao_andes",
    widget_id: "widget-1",
  };
  return placeholders[field] ?? field;
}

function LimitsPanel({ rate, concurrency }: { rate: number; concurrency: number }) {
  return (
    <div className="grid grid-cols-2 gap-px border border-line bg-line">
      <div className="bg-panel p-4">
        <p className="field-label">Quota</p>
        <p className="metric-value mt-2 text-xl font-semibold">{rate}/hora</p>
      </div>
      <div className="bg-panel p-4">
        <p className="field-label">Concorrência</p>
        <p className="metric-value mt-2 text-xl font-semibold">{concurrency} runs</p>
      </div>
    </div>
  );
}

function WaitingPanel() {
  return (
    <Panel className="grid min-h-[34rem] place-items-center">
      <div className="max-w-md px-8 text-center">
        <Braces className="mx-auto text-signal" size={32} strokeWidth={1.4} />
        <h2 className="mt-5 text-xl font-semibold">Aguardando uma solicitação</h2>
        <p className="mt-2 text-sm leading-6 text-muted">
          O resultado ficará somente no estado desta página. Nenhuma memória conversacional será
          criada entre as runs.
        </p>
      </div>
    </Panel>
  );
}

function RunError({ error, retry }: { error: Error; retry: () => void }) {
  const apiError = error instanceof ApiError ? error : null;
  const presentation: Record<string, { title: string; message: string }> = {
    AUTH_REQUIRED: {
      title: "Acesso recusado",
      message: "A sessão não possui um token Bearer válido.",
    },
    AUTH_INVALID: {
      title: "Acesso recusado",
      message: "O token desta sessão não corresponde ao segredo do backend.",
    },
    RUN_RATE_LIMITED: {
      title: "Quota horária atingida",
      message: "Três runs já foram aceitas nesta janela. Aguarde o reset antes de tentar novamente.",
    },
    RUN_CONCURRENCY_LIMIT: {
      title: "Duas runs já estão ativas",
      message: "A tentativa foi recusada antes de consumir quota. Tente novamente em instantes.",
    },
    MODEL_NOT_CONFIGURED: {
      title: "Modelo indisponível",
      message: "A chave da Groq precisa ser configurada somente no backend.",
    },
    API_UNREACHABLE: {
      title: "Backend em cold start",
      message: "O serviço gratuito pode estar acordando. Aguarde cerca de um minuto.",
    },
  };
  const content = apiError ? presentation[apiError.code] : undefined;
  return (
    <ErrorState
      title={content?.title ?? "Run não concluída"}
      message={content?.message ?? error.message}
      retry={retry}
    />
  );
}

function RunResult({ result }: { result: PublicRunResult }) {
  const partial = result.status !== "completed";
  const policySummary = result.policy_decisions.reduce(
    (totals, policy) => ({ ...totals, [policy.outcome]: (totals[policy.outcome] ?? 0) + 1 }),
    {} as Record<string, number>,
  );
  const citedEvidence = result.evidence.filter((item) => result.evidence_ids.includes(item.id)).length;
  return (
    <div className="space-y-6">
      {partial ? (
        <div className="flex gap-3 border border-signal/30 bg-signal/[0.06] p-4" role="note">
          <AlertTriangle className="mt-0.5 shrink-0 text-signal" size={18} />
          <div>
            <p className="text-sm font-semibold">Execução parcial</p>
            <p className="mt-1 text-xs leading-5 text-muted">
              O agente devolveu o que conseguiu observar antes de {result.metrics.termination_reason}.
            </p>
          </div>
        </div>
      ) : null}

      <Panel>
        <PanelHeader className="flex flex-wrap items-center justify-between gap-3">
          <PanelTitle>Resposta fundamentada</PanelTitle>
          <div className="flex flex-wrap justify-end gap-2">
            <Badge tone={partial ? "warning" : "good"}>{result.status}</Badge>
            {result.intent_id ? <Badge tone="neutral">{result.intent_id}</Badge> : null}
            <Badge tone="info">{result.decision}</Badge>
            <Button asChild variant="outline" size="sm">
              <Link href={`/trace?run_id=${encodeURIComponent(result.run_id)}`}>
                Ver trace <ArrowUpRight size={12} />
              </Link>
            </Button>
          </div>
        </PanelHeader>
        <PanelContent>
          <p className="text-lg leading-8 text-foreground">{result.answer}</p>
          {result.uncertainties.length ? (
            <ul className="mt-5 space-y-2 border-t border-line pt-4 text-xs text-muted">
              {result.uncertainties.map((uncertainty) => (
                <li key={uncertainty}>— {uncertainty}</li>
              ))}
            </ul>
          ) : null}
        </PanelContent>
      </Panel>

      <div className="grid gap-px border border-line bg-line sm:grid-cols-3">
        <ResultMetric
          icon={FileSearch}
          label="Evidências citadas"
          value={`${citedEvidence}/${result.evidence.length}`}
        />
        <ResultMetric
          icon={Route}
          label="Policy"
          value={Object.entries(policySummary)
            .map(([outcome, count]) => `${count} ${outcome}`)
            .join(" · ") || "sem decisão"}
        />
        <ResultMetric
          icon={ShieldCheck}
          label="Término"
          value={result.metrics.termination_reason}
        />
      </div>

      <Panel>
        <PanelHeader>
          <PanelTitle>Timeline de tools e policy</PanelTitle>
        </PanelHeader>
        {result.tool_calls.length ? (
          <div className="divide-y divide-line">
            {result.tool_calls.map((tool) => {
              const policy = result.policy_decisions.find(
                (item) => item.tool_sequence === tool.sequence,
              );
              return (
                <div key={tool.sequence} className="grid gap-4 px-5 py-4 md:grid-cols-[2rem_1fr_auto]">
                  <span className="grid size-7 place-items-center rounded-full border border-line font-mono text-[10px] text-signal">
                    {tool.sequence}
                  </span>
                  <div className="min-w-0">
                    <p className="font-mono text-xs font-semibold text-foreground">
                      {tool.tool_alias}
                    </p>
                    <p className="mt-1 text-[11px] text-muted">
                      {tool.outcome} · {tool.latency_ms.toFixed(1)} ms · {tool.evidence_id ?? "sem evidência"}
                    </p>
                    {policy?.reason_codes.length ? (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {policy.reason_codes.map((code) => (
                          <Badge key={code} tone={policy.outcome === "block" ? "danger" : "good"}>
                            {code}
                          </Badge>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  <Badge tone={policy?.outcome === "block" ? "danger" : "neutral"}>
                    {policy?.outcome ?? tool.status}
                  </Badge>
                </div>
              );
            })}
          </div>
        ) : (
          <PanelContent>
            <p className="text-sm text-muted">A run terminou sem chamar tools.</p>
          </PanelContent>
        )}
      </Panel>

      <Panel>
        <PanelHeader className="flex items-center justify-between gap-3">
          <PanelTitle>Evidências redigidas</PanelTitle>
          <Badge tone="neutral">{result.evidence.length} itens</Badge>
        </PanelHeader>
        <div className="divide-y divide-line">
          {result.evidence.map((evidence) => (
            <details key={evidence.id} className="group px-5 py-4">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4">
                <span>
                  <span className="font-mono text-xs font-semibold text-signal">{evidence.id}</span>
                  <span className="ml-3 text-xs text-muted">{evidence.mcp_tool_name}</span>
                </span>
                <Badge tone={evidence.truncated ? "warning" : "neutral"}>
                  {evidence.truncated ? "truncada" : evidence.outcome}
                </Badge>
              </summary>
              <pre className="mt-4 max-h-80 overflow-auto border border-line bg-ink p-4 font-mono text-[10px] leading-5 text-muted">
                {JSON.stringify(evidence.result, null, 2)}
              </pre>
            </details>
          ))}
        </div>
      </Panel>

      <div className="grid grid-cols-2 gap-px border border-line bg-line sm:grid-cols-4">
        <ResultMetric icon={Clock3} label="Latência" value={`${result.metrics.latency_ms.toFixed(0)} ms`} />
        <ResultMetric icon={Activity} label="Modelo" value={`${result.metrics.model_calls} calls`} />
        <ResultMetric icon={TerminalSquare} label="Tools" value={String(result.metrics.tool_calls)} />
        <ResultMetric icon={CircleStop} label="Tokens" value={`${result.metrics.total_tokens} tokens`} />
      </div>
    </div>
  );
}

function ResultMetric({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
}) {
  return (
    <div className="bg-panel p-4">
      <Icon size={14} className="text-signal" />
      <p className="field-label mt-3">{label}</p>
      <p className="metric-value mt-2 text-lg font-semibold">{value}</p>
    </div>
  );
}
