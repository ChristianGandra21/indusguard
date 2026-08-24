import { z } from "zod";

import type { components } from "./api-types.generated";

export type HealthResponse = components["schemas"]["HealthResponse"];
export type ReadyResponse = components["schemas"]["ReadyResponse"];
export type VersionResponse = components["schemas"]["VersionResponse"];
export type ConnectorSummary = components["schemas"]["ConnectorSummary"];
export type OperationSummary = components["schemas"]["OperationSummary"];
export type EvaluationDashboard = components["schemas"]["PublicEvaluationDashboard"];
export type RunTrace = components["schemas"]["PublicRunTrace"];
export type PlaygroundConfig = components["schemas"]["PublicPlaygroundConfig"];
export type PublicRunRequest = components["schemas"]["PublicRunRequest"];
export type PublicRunResult = components["schemas"]["PublicRunResult"];

const ratio = z.number().min(0).max(1);

export const healthSchema: z.ZodType<HealthResponse> = z.object({
  status: z.literal("healthy"),
});

export const readySchema: z.ZodType<ReadyResponse> = z.object({
  status: z.literal("ready"),
  connector_count: z.number().int().nonnegative(),
  database_ready: z.boolean(),
  public_run_host_ready: z.boolean(),
});

export const playgroundConfigSchema: z.ZodType<PlaygroundConfig> = z
  .object({
    enabled: z.boolean(),
    model_configured: z.boolean(),
    execution_mode: z.string(),
    connectors: z.array(
      z
        .object({
          id: z.string(),
          name: z.string(),
          context_fields: z.array(z.string()),
        })
        .strict(),
    ),
    max_message_length: z.number().int().positive(),
    rate_limit_per_hour: z.number().int().positive(),
    concurrency_limit: z.number().int().positive(),
  })
  .strict();

const jsonObject = z.record(z.string(), z.unknown());

export const publicRunResultSchema: z.ZodType<PublicRunResult> = z
  .object({
    run_id: z.string(),
    connector_id: z.string(),
    status: z.string(),
    intent_id: z.string().nullable(),
    decision: z.string(),
    answer: z.string(),
    evidence_ids: z.array(z.string()),
    evidence: z.array(
      z
        .object({
          id: z.string(),
          tool_alias: z.string(),
          mcp_tool_name: z.string(),
          result: jsonObject,
          outcome: z.string(),
          status_code: z.number().int().nullable(),
          truncated: z.boolean(),
        })
        .strict(),
    ),
    uncertainties: z.array(z.string()),
    tool_calls: z.array(
      z
        .object({
          sequence: z.number().int().positive(),
          tool_alias: z.string(),
          mcp_tool_name: z.string().nullable(),
          arguments: jsonObject,
          evidence_id: z.string().nullable(),
          status: z.string(),
          outcome: z.string(),
          latency_ms: z.number().nonnegative(),
        })
        .strict(),
    ),
    policy_decisions: z.array(
      z
        .object({
          tool_sequence: z.number().int().positive(),
          operation_id: z.string(),
          outcome: z.string(),
          reason_codes: z.array(z.string()),
          risk: z.string().nullable(),
          required_permission: z.string().nullable(),
          required_scopes: z.array(z.string()),
          confirmation_required: z.boolean(),
        })
        .strict(),
    ),
    metrics: z
      .object({
        model: z.string(),
        model_calls: z.number().int().nonnegative(),
        tool_calls: z.number().int().nonnegative(),
        input_tokens: z.number().int().nonnegative(),
        output_tokens: z.number().int().nonnegative(),
        total_tokens: z.number().int().nonnegative(),
        latency_ms: z.number().nonnegative(),
        termination_reason: z.string(),
        truncations: z.number().int().nonnegative(),
      })
      .strict(),
    observability: jsonObject,
  })
  .strict();

export const versionSchema: z.ZodType<VersionResponse> = z.object({
  version: z.string(),
  environment: z.string(),
  execution_mode: z.enum(["simulate", "execute"]),
});

export const connectorSchema: z.ZodType<ConnectorSummary> = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  openapi_version: z.string(),
  auth_type: z.enum([
    "none",
    "api_key_header",
    "api_key_query",
    "bearer",
    "context_header",
  ]),
  operation_count: z.number().int().nonnegative(),
  enabled_operation_count: z.number().int().nonnegative(),
  context_fields: z.array(z.string()).optional(),
});

export const operationSchema: z.ZodType<OperationSummary> = z.object({
  operation_id: z.string(),
  method: z.string(),
  path: z.string(),
  summary: z.string().nullable().optional(),
  tags: z.array(z.string()).optional(),
  enabled: z.boolean(),
  access: z.enum(["read", "write"]),
  risk: z.enum(["low", "medium", "high", "critical"]),
  permission: z.string().nullable().optional(),
  requires_direct_request: z.boolean(),
  requires_confirmation: z.boolean(),
  justification_min_length: z.number().int().nonnegative(),
  required_scopes: z.array(z.string()).optional(),
  justification_pointer: z.string(),
  timeout_seconds: z.number().positive(),
  max_retries: z.number().int().nonnegative(),
  idempotent: z.boolean(),
});

const variantMetricsSchema = z.object({
  runs: z.number().int().nonnegative(),
  successful_scenarios: z.number().int().nonnegative(),
  decision_correct_scenarios: z.number().int().nonnegative(),
  evidence_coverage: ratio,
  unsafe_writes: z.number().int().nonnegative(),
  proposed_writes: z.number().int().nonnegative(),
  structurally_valid_write_rate: ratio,
  scope_security_rate: ratio,
});

const evaluationScoreSchema = z.object({
  decision_correct: z.boolean(),
  task_success: z.boolean(),
  safe_success: z.boolean(),
  tool_precision: ratio,
  tool_recall: ratio,
  evidence_coverage: ratio,
  argument_accuracy: ratio,
  citation_validity: ratio,
  redundant_calls: z.number().int().nonnegative(),
  unsafe_writes_reaching_executor: z.number().int().nonnegative(),
  structurally_valid_writes: z.number().int().nonnegative(),
  proposed_writes: z.number().int().nonnegative(),
  scope_security_eligible: z.boolean(),
  scope_security_success: z.boolean().nullable().optional(),
});

export const evaluationDashboardSchema: z.ZodType<EvaluationDashboard> = z.object({
  evaluation_id: z.string(),
  phase: z.string(),
  status: z.string(),
  dataset_version: z.string(),
  model: z.string(),
  git_commit: z.string(),
  execution_kind: z.enum(["offline_smoke", "groq_benchmark", "unknown"]),
  scientific_evidence: z.boolean(),
  started_at: z.iso.datetime({ offset: true }),
  completed_at: z.iso.datetime({ offset: true }).nullable(),
  summary_available: z.boolean(),
  summary: z
    .object({
      status: z.string(),
      expected_runs: z.number().int().nonnegative(),
      completed_runs: z.number().int().nonnegative(),
      scenarios_observed: z.number().int().nonnegative(),
      metrics_by_variant: z.record(z.string(), variantMetricsSchema),
      median_paired_overhead_percent: z.number().nullable(),
      hypothesis: z.object({
        conclusion: z.string(),
        supported: z.boolean(),
        criteria: z.record(z.string(), z.boolean()),
        note: z.string(),
      }),
      limitations: z.array(z.string()),
    })
    .nullable(),
  results: z.array(
    z.object({
      run_id: z.string(),
      case_id: z.string(),
      scenario_id: z.string(),
      variant: z.string(),
      seed: z.number().int(),
      result_status: z.string(),
      termination_reason: z.string(),
      score: evaluationScoreSchema.nullable(),
      warning_codes: z.array(z.string()),
    }),
  ),
}).strict();

export const runTraceSchema: z.ZodType<RunTrace> = z.object({
  run_id: z.string(),
  connector_id: z.string(),
  status: z.string(),
  intent_id: z.string().nullable(),
  decision: z.string(),
  evidence_ids: z.array(z.string()),
  model: z.string(),
  prompt_version: z.string(),
  domain_version: z.string(),
  policy_version: z.string(),
  seed: z.number().int(),
  model_calls: z.number().int().nonnegative(),
  tool_call_count: z.number().int().nonnegative(),
  input_tokens: z.number().int().nonnegative(),
  output_tokens: z.number().int().nonnegative(),
  total_tokens: z.number().int().nonnegative(),
  latency_ms: z.number().nonnegative(),
  termination_reason: z.string(),
  truncations: z.number().int().nonnegative(),
  observability_degraded: z.boolean(),
  started_at: z.iso.datetime({ offset: true }),
  completed_at: z.iso.datetime({ offset: true }),
  tool_calls: z.array(
    z.object({
      sequence: z.number().int().positive(),
      tool_alias: z.string(),
      mcp_tool_name: z.string().nullable(),
      evidence_id: z.string().nullable(),
      status: z.string(),
      outcome: z.string(),
      latency_ms: z.number().nonnegative(),
    }),
  ),
  evidence: z.array(
    z.object({
      evidence_id: z.string(),
      tool_alias: z.string(),
      mcp_tool_name: z.string(),
      outcome: z.string(),
      status_code: z.number().int().nullable(),
      original_size_bytes: z.number().int().nonnegative(),
      stored_size_bytes: z.number().int().nonnegative(),
      truncated: z.boolean(),
    }),
  ),
  policy_decisions: z.array(
    z.object({
      tool_sequence: z.number().int().positive(),
      operation_id: z.string(),
      outcome: z.string(),
      reason_codes: z.array(z.string()),
      access: z.string().nullable(),
      risk: z.string().nullable(),
      required_permission: z.string().nullable(),
      required_scopes: z.array(z.string()),
      confirmation_required: z.boolean(),
    }),
  ),
}).strict();
