import { z } from "zod";

import {
  connectorSchema,
  evaluationDashboardSchema,
  healthSchema,
  operationSchema,
  playgroundConfigSchema,
  publicRunResultSchema,
  readySchema,
  recentRunSummarySchema,
  runTraceSchema,
  versionSchema,
} from "./schemas";
import type { PublicRunRequest } from "./schemas";

const apiBaseUrl = (process.env.NEXT_PUBLIC_INDUSGUARD_API_URL ?? "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function get<Schema extends z.ZodType>(path: string, schema: Schema): Promise<z.output<Schema>> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, {
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new ApiError(0, "API_UNREACHABLE", "Não foi possível alcançar o backend IndusGuard.");
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const parsed = z
      .object({ detail: z.object({ code: z.string(), message: z.string() }) })
      .safeParse(body);
    throw new ApiError(
      response.status,
      parsed.success ? parsed.data.detail.code : "API_ERROR",
      parsed.success ? parsed.data.detail.message : "O backend devolveu uma resposta inesperada.",
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(502, "CONTRACT_INVALID", "A resposta não corresponde ao contrato esperado.");
  }
  return parsed.data;
}

async function post<Schema extends z.ZodType>(
  path: string,
  payload: unknown,
  token: string,
  schema: Schema,
): Promise<z.output<Schema>> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}${path}`, {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError(0, "API_UNREACHABLE", "Não foi possível alcançar o backend IndusGuard.");
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const parsed = z
      .object({ detail: z.object({ code: z.string(), message: z.string() }) })
      .safeParse(body);
    throw new ApiError(
      response.status,
      parsed.success ? parsed.data.detail.code : "API_ERROR",
      parsed.success ? parsed.data.detail.message : "O backend devolveu uma resposta inesperada.",
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(502, "CONTRACT_INVALID", "A resposta não corresponde ao contrato esperado.");
  }
  return parsed.data;
}

export const api = {
  health: () => get("/api/v1/health", healthSchema),
  ready: () => get("/api/v1/ready", readySchema),
  version: () => get("/api/v1/version", versionSchema),
  connectors: () => get("/api/v1/connectors", z.array(connectorSchema)),
  operations: (connectorId: string) =>
    get(`/api/v1/connectors/${encodeURIComponent(connectorId)}/operations`, z.array(operationSchema)),
  latestEvaluation: () => get("/api/v1/evaluations/latest", evaluationDashboardSchema),
  recentRuns: () => get("/api/v1/runs/recent", z.array(recentRunSummarySchema)),
  trace: (runId: string) =>
    get(`/api/v1/runs/${encodeURIComponent(runId)}/trace`, runTraceSchema),
  playgroundConfig: () => get("/api/v1/playground/config", playgroundConfigSchema),
  run: (payload: PublicRunRequest, token: string) =>
    post("/api/v1/runs", payload, token, publicRunResultSchema),
};
