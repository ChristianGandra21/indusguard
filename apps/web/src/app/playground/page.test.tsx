import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "@/lib/api";
import type { PlaygroundConfig, PublicRunResult } from "@/lib/schemas";

import PlaygroundPage from "./page";

const config: PlaygroundConfig = {
  enabled: true,
  model_configured: true,
  execution_mode: "simulate",
  connectors: [
    {
      id: "synthetic",
      name: "API sintética de extensibilidade",
      context_fields: ["widget_id"],
    },
  ],
  max_message_length: 2000,
  rate_limit_per_hour: 3,
  concurrency_limit: 2,
};

const result: PublicRunResult = {
  run_id: "11111111-1111-4111-8111-111111111111",
  connector_id: "synthetic",
  status: "completed",
  intent_id: "consultar",
  decision: "orient",
  answer: "O widget está ativo [ev-001].",
  evidence_ids: ["ev-001"],
  evidence: [
    {
      id: "ev-001",
      tool_alias: "synthetic__getWidget",
      mcp_tool_name: "synthetic.getWidget",
      result: { execution: { outcome: "executed", data: { status: "active" } } },
      outcome: "executed",
      status_code: 200,
      truncated: false,
    },
  ],
  uncertainties: [],
  tool_calls: [
    {
      sequence: 1,
      tool_alias: "synthetic__getWidget",
      mcp_tool_name: "synthetic.getWidget",
      arguments: { path: { widgetId: "widget-1" } },
      evidence_id: "ev-001",
      status: "success",
      outcome: "executed",
      latency_ms: 2.5,
    },
  ],
  policy_decisions: [
    {
      tool_sequence: 1,
      operation_id: "getWidget",
      outcome: "allow",
      reason_codes: ["READ_APPROVED"],
      risk: "low",
      required_permission: null,
      required_scopes: [],
      confirmation_required: false,
    },
  ],
  metrics: {
    model: "scripted-e2e-model",
    model_calls: 3,
    tool_calls: 1,
    input_tokens: 10,
    output_tokens: 5,
    total_tokens: 15,
    latency_ms: 12.5,
    termination_reason: "COMPLETED",
    truncations: 0,
  },
  observability: { status: "healthy" },
};

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PlaygroundPage />
    </QueryClientProvider>,
  );
}

describe("playground protegido", () => {
  it("mantém um estado explícito enquanto o backend acorda", () => {
    vi.spyOn(api, "playgroundConfig").mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByRole("status")).toHaveTextContent("Acordando o plano de controle");
  });

  it("mantém o token na sessão e mostra resposta, evidência, policy e métricas", async () => {
    vi.spyOn(api, "playgroundConfig").mockResolvedValue(config);
    const run = vi.spyOn(api, "run").mockResolvedValue(result);
    const user = userEvent.setup();

    renderPage();

    expect(
      await screen.findByRole("heading", { name: "Teste o agente. Preserve a fronteira." }),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Token do proprietário"), "owner-session-token");
    await user.click(screen.getByRole("button", { name: "Salvar acesso nesta sessão" }));
    expect(sessionStorage.getItem("indusguard.owner_token")).toBe("owner-session-token");

    await user.type(screen.getByLabelText("ID do widget"), "widget-1");
    await user.type(screen.getByLabelText("Solicitação"), "Qual é o estado do widget?");
    await user.click(screen.getByRole("button", { name: "Executar agente protegido" }));

    expect(await screen.findByText("O widget está ativo [ev-001].")).toBeInTheDocument();
    expect(screen.getByText("READ_APPROVED")).toBeInTheDocument();
    expect(screen.getByText("synthetic__getWidget")).toBeInTheDocument();
    expect(screen.getByText("15 tokens")).toBeInTheDocument();
    expect(run).toHaveBeenCalledWith(
      {
        connector_id: "synthetic",
        message: "Qual é o estado do widget?",
        seed: 42,
        context: { widget_id: "widget-1" },
        direct_request: false,
      },
      "owner-session-token",
    );

    await user.click(screen.getByRole("button", { name: "Encerrar sessão" }));
    expect(sessionStorage.getItem("indusguard.owner_token")).toBeNull();
    expect(screen.queryByText("O widget está ativo [ev-001].")).not.toBeInTheDocument();
  });

  it("diferencia token inválido de quota e mantém o segredo fora da tela", async () => {
    vi.spyOn(api, "playgroundConfig").mockResolvedValue(config);
    vi.spyOn(api, "run").mockRejectedValue(
      new ApiError(401, "AUTH_INVALID", "O token Bearer informado é inválido."),
    );
    sessionStorage.setItem("indusguard.owner_token", "token-que-nao-pode-aparecer");
    const user = userEvent.setup();

    renderPage();
    await user.type(await screen.findByLabelText("ID do widget"), "widget-1");
    await user.type(screen.getByLabelText("Solicitação"), "Consulte o widget.");
    await user.click(screen.getByRole("button", { name: "Executar agente protegido" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Acesso recusado");
    expect(document.body).not.toHaveTextContent("token-que-nao-pode-aparecer");
  });

  it("apresenta indisponibilidade do modelo sem inventar resultado", async () => {
    vi.spyOn(api, "playgroundConfig").mockResolvedValue({ ...config, model_configured: false });
    sessionStorage.setItem("indusguard.owner_token", "owner-session-token");

    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Modelo ainda não configurado");
    await waitFor(() => expect(api.playgroundConfig).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: "Executar agente protegido" })).not.toBeInTheDocument();
  });

  it("distingue quota horária de falha de autenticação", async () => {
    vi.spyOn(api, "playgroundConfig").mockResolvedValue(config);
    vi.spyOn(api, "run").mockRejectedValue(
      new ApiError(429, "RUN_RATE_LIMITED", "Limite atingido."),
    );
    sessionStorage.setItem("indusguard.owner_token", "owner-session-token");
    const user = userEvent.setup();

    renderPage();
    await user.type(await screen.findByLabelText("ID do widget"), "widget-1");
    await user.type(screen.getByLabelText("Solicitação"), "Consulte o widget.");
    await user.click(screen.getByRole("button", { name: "Executar agente protegido" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Quota horária atingida");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Acesso recusado");
  });
});
