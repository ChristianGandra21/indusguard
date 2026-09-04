import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RecentRunSummary, RunTrace } from "@/lib/schemas";

import { TraceContent } from "./trace-content";

const navigation = vi.hoisted(() => ({
  push: vi.fn(),
  searchParams: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: navigation.push }),
  useSearchParams: () => navigation.searchParams,
}));

const runId = "11111111-1111-4111-8111-111111111111";

const recentRun: RecentRunSummary = {
  run_id: runId,
  connector_id: "synthetic",
  status: "completed",
  intent_id: "consultar",
  decision: "orient",
  termination_reason: "COMPLETED",
  model: "scripted-e2e-model",
  started_at: "2026-08-24T12:00:00Z",
  completed_at: "2026-08-24T12:00:02Z",
};

const trace: RunTrace = {
  run_id: runId,
  connector_id: "synthetic",
  status: "completed",
  intent_id: "consultar",
  decision: "orient",
  evidence_ids: ["ev-001"],
  model: "scripted-e2e-model",
  prompt_version: "prompt-v1",
  domain_version: "domain-v1",
  policy_version: "policy-v1",
  seed: 42,
  model_calls: 1,
  tool_call_count: 1,
  input_tokens: 8,
  output_tokens: 4,
  total_tokens: 12,
  latency_ms: 25,
  termination_reason: "COMPLETED",
  truncations: 0,
  observability_degraded: false,
  started_at: "2026-08-24T12:00:00Z",
  completed_at: "2026-08-24T12:00:02Z",
  tool_calls: [
    {
      sequence: 1,
      tool_alias: "synthetic__getWidget",
      mcp_tool_name: "synthetic.getWidget",
      evidence_id: "ev-001",
      status: "success",
      outcome: "executed",
      latency_ms: 12,
    },
  ],
  evidence: [
    {
      evidence_id: "ev-001",
      tool_alias: "synthetic__getWidget",
      mcp_tool_name: "synthetic.getWidget",
      outcome: "executed",
      status_code: 200,
      original_size_bytes: 128,
      stored_size_bytes: 128,
      truncated: false,
    },
  ],
  policy_decisions: [
    {
      tool_sequence: 1,
      operation_id: "getWidget",
      outcome: "allow",
      reason_codes: ["READ_APPROVED"],
      access: "read",
      risk: "low",
      required_permission: null,
      required_scopes: [],
      confirmation_required: false,
    },
  ],
};

afterEach(() => {
  cleanup();
  navigation.push.mockReset();
  navigation.searchParams = new URLSearchParams();
  vi.restoreAllMocks();
});

function renderTrace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TraceContent />
    </QueryClientProvider>,
  );
}

describe("trace público", () => {
  it("oferece dropdown de runs recentes sem exigir UUID de cabeça", async () => {
    vi.spyOn(api, "recentRuns").mockResolvedValue([recentRun]);
    vi.spyOn(api, "trace").mockResolvedValue(trace);
    const user = userEvent.setup();

    renderTrace();

    await screen.findByRole("option", { name: /synthetic/ });
    await user.selectOptions(screen.getByLabelText("Runs recentes"), runId);

    expect(navigation.push).toHaveBeenCalledWith(`/trace?run_id=${encodeURIComponent(runId)}`);
  });

  it("carrega o trace selecionado pela URL", async () => {
    navigation.searchParams = new URLSearchParams({ run_id: runId });
    vi.spyOn(api, "recentRuns").mockResolvedValue([recentRun]);
    vi.spyOn(api, "trace").mockResolvedValue(trace);

    renderTrace();

    expect(await screen.findByText("synthetic__getWidget")).toBeInTheDocument();
    expect(screen.getByLabelText("Runs recentes")).toHaveValue(runId);
    expect(screen.getByLabelText("ID da run")).toHaveValue(runId);
  });

  it("mantém o carregamento manual por run_id", async () => {
    vi.spyOn(api, "recentRuns").mockResolvedValue([]);
    const user = userEvent.setup();

    renderTrace();

    await user.type(await screen.findByLabelText("ID da run"), runId);
    await user.click(screen.getByRole("button", { name: /Carregar trace/ }));

    await waitFor(() =>
      expect(navigation.push).toHaveBeenCalledWith(`/trace?run_id=${encodeURIComponent(runId)}`),
    );
  });
});
