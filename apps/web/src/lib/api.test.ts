import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

const trace = {
  run_id: "11111111-1111-4111-8111-111111111111",
  connector_id: "synthetic",
  status: "completed",
  intent_id: "inspect",
  decision: "orient",
  evidence_ids: ["ev-001"],
  model: "fake",
  prompt_version: "agent-v1",
  domain_version: "1",
  policy_version: "policy-v1",
  seed: 42,
  model_calls: 3,
  tool_call_count: 1,
  input_tokens: 10,
  output_tokens: 5,
  total_tokens: 15,
  latency_ms: 12.5,
  termination_reason: "COMPLETED",
  truncations: 0,
  observability_degraded: false,
  started_at: "2026-08-24T12:00:00Z",
  completed_at: "2026-08-24T12:00:01Z",
  tool_calls: [],
  evidence: [],
  policy_decisions: [],
};

afterEach(() => vi.restoreAllMocks());

describe("cliente público", () => {
  it("aceita uma projeção de trace válida", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(trace), { status: 200, headers: { "content-type": "application/json" } }),
    );

    await expect(api.trace(trace.run_id)).resolves.toMatchObject({ run_id: trace.run_id });
  });

  it("rejeita conteúdo livre que não pertence ao contrato público", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ...trace, request_message: "não pode chegar ao browser" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(api.trace(trace.run_id)).rejects.toMatchObject({ code: "CONTRACT_INVALID" });
  });

  it("preserva códigos estáveis de erro do backend", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ detail: { code: "TRACE_NOT_FOUND", message: "Trace não encontrado." } }),
        { status: 404, headers: { "content-type": "application/json" } },
      ),
    );

    const error = await api.trace("missing").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: "TRACE_NOT_FOUND" });
  });

  it("envia o token somente no header da run e desativa cache", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          run_id: "run-1",
          connector_id: "synthetic",
          status: "completed",
          intent_id: "consultar",
          decision: "orient",
          answer: "O widget está ativo [ev-001].",
          evidence_ids: ["ev-001"],
          evidence: [],
          uncertainties: [],
          tool_calls: [],
          policy_decisions: [],
          metrics: {
            model: "fake",
            model_calls: 3,
            tool_calls: 1,
            input_tokens: 10,
            output_tokens: 5,
            total_tokens: 15,
            latency_ms: 12,
            termination_reason: "COMPLETED",
            truncations: 0,
          },
          observability: { status: "healthy" },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    await api.run(
      {
        connector_id: "synthetic",
        message: "Consulte o widget.",
        seed: 42,
        context: { widget_id: "widget-1" },
        direct_request: false,
      },
      "owner-secret-token",
    );

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).not.toContain("owner-secret-token");
    expect(init).toMatchObject({
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: "Bearer owner-secret-token",
        "Content-Type": "application/json",
      },
    });
  });
});
