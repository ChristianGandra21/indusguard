import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "@/lib/api";
import type { ConnectorSummary, OperationSummary } from "@/lib/schemas";

import ConnectorsPage from "./page";

const connector: ConnectorSummary = {
  id: "synthetic",
  name: "Synthetic API",
  description: "Conector de prova.",
  openapi_version: "3.1.0",
  auth_type: "none",
  operation_count: 2,
  enabled_operation_count: 2,
  context_fields: [],
};

const baseOperation = {
  path: "/widgets",
  summary: "Operação sintética",
  tags: [],
  enabled: true,
  risk: "low",
  permission: null,
  requires_direct_request: false,
  requires_confirmation: false,
  justification_min_length: 0,
  required_scopes: [],
  justification_pointer: "/justification",
  timeout_seconds: 10,
  max_retries: 0,
  idempotent: true,
} satisfies Omit<OperationSummary, "operation_id" | "method" | "access">;

const operations: OperationSummary[] = [
  { ...baseOperation, operation_id: "getWidget", method: "GET", access: "read" },
  { ...baseOperation, operation_id: "updateWidget", method: "PATCH", access: "write", risk: "high" },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConnectorsPage />
    </QueryClientProvider>,
  );
}

describe("página de conectores", () => {
  it("expõe filtros acessíveis e separa leituras de escritas", async () => {
    vi.spyOn(api, "connectors").mockResolvedValue([connector]);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    const user = userEvent.setup();

    renderPage();

    expect(await screen.findByRole("heading", { name: "Capacidades declaradas. Permissões explícitas." })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Synthetic API/ })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("getWidget")).toBeInTheDocument();
    expect(screen.getByText("updateWidget")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Leituras" }));
    expect(screen.getByText("getWidget")).toBeInTheDocument();
    expect(screen.queryByText("updateWidget")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Escritas" }));
    expect(screen.queryByText("getWidget")).not.toBeInTheDocument();
    expect(screen.getByText("updateWidget")).toBeInTheDocument();
  });

  it("mostra estado vazio quando o catálogo não tem conectores", async () => {
    vi.spyOn(api, "connectors").mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("Nenhum conector")).toBeInTheDocument();
  });

  it("mantém a indisponibilidade da API diferente do estado vazio", async () => {
    vi.spyOn(api, "connectors").mockRejectedValue(
      new ApiError(0, "API_UNREACHABLE", "Não foi possível alcançar o backend IndusGuard."),
    );

    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Backend fora de alcance");
    expect(screen.queryByText("Nenhum conector")).not.toBeInTheDocument();
  });
});
