import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "@/lib/api";
import type { EvaluationDashboard } from "@/lib/schemas";

import EvaluationsPage from "./page";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Bar: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

const metrics = {
  runs: 1,
  successful_scenarios: 1,
  decision_correct_scenarios: 1,
  evidence_coverage: 1,
  unsafe_writes: 0,
  proposed_writes: 0,
  structurally_valid_write_rate: 1,
  scope_security_rate: 1,
};

const evaluation: EvaluationDashboard = {
  evaluation_id: "22222222-2222-4222-8222-222222222222",
  phase: "pilot",
  status: "partial",
  dataset_version: "official-v1",
  model: "scripted-eval-smoke",
  git_commit: "abc123",
  execution_kind: "offline_smoke",
  scientific_evidence: false,
  started_at: "2026-08-24T12:00:00Z",
  completed_at: "2026-08-24T12:00:01Z",
  summary_available: true,
  summary: {
    status: "partial",
    expected_runs: 12,
    completed_runs: 6,
    scenarios_observed: 2,
    metrics_by_variant: { prompt_only: metrics, guarded: metrics },
    median_paired_overhead_percent: 4.2,
    hypothesis: {
      conclusion: "partial",
      supported: false,
      criteria: { complete_benchmark: false },
      note: "Avaliação parcial.",
    },
    limitations: ["Modelo fake, sem valor científico."],
  },
  results: [],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EvaluationsPage />
    </QueryClientProvider>,
  );
}

describe("página de avaliações", () => {
  it("destaca smoke fake e avaliação parcial", async () => {
    vi.spyOn(api, "latestEvaluation").mockResolvedValue(evaluation);

    renderPage();

    expect(await screen.findByText("smoke offline")).toBeInTheDocument();
    expect(screen.getByText("Este resultado não sustenta a hipótese.")).toBeInTheDocument();
    expect(screen.getAllByText("partial")).toHaveLength(2);
  });

  it("distingue um benchmark Groq de um smoke offline", async () => {
    vi.spyOn(api, "latestEvaluation").mockResolvedValue({
      ...evaluation,
      status: "completed",
      execution_kind: "groq_benchmark",
      scientific_evidence: true,
    });

    renderPage();

    expect(await screen.findByText("benchmark Groq")).toBeInTheDocument();
    expect(screen.queryByText("Este resultado não sustenta a hipótese.")).not.toBeInTheDocument();
  });

  it("explica quando a avaliação existe mas ainda não possui resumo", async () => {
    vi.spyOn(api, "latestEvaluation").mockResolvedValue({
      ...evaluation,
      summary_available: false,
      summary: null,
    });

    renderPage();

    expect(await screen.findByText("Resumo ainda indisponível")).toBeInTheDocument();
  });

  it("mostra estado vazio quando o banco não possui avaliações", async () => {
    vi.spyOn(api, "latestEvaluation").mockRejectedValue(
      new ApiError(404, "EVALUATION_NOT_FOUND", "Nenhuma avaliação foi registrada ainda."),
    );

    renderPage();

    expect(await screen.findByText("Nenhuma avaliação registrada")).toBeInTheDocument();
    expect(screen.getByText("make eval-pilot-fake")).toBeInTheDocument();
  });

  it("distingue indisponibilidade do banco de um estado vazio", async () => {
    vi.spyOn(api, "latestEvaluation").mockRejectedValue(
      new ApiError(503, "DATASTORE_UNAVAILABLE", "Banco temporariamente indisponível."),
    );

    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Banco temporariamente indisponível.");
    expect(screen.queryByText("Nenhuma avaliação registrada")).not.toBeInTheDocument();
  });
});
