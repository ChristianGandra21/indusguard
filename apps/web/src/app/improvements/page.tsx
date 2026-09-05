"use client";

import { useRef, useState } from "react";
import { GitCommitHorizontal, LockKeyhole, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelContent, PanelHeader, PanelTitle } from "@/components/ui/panel";
import { api } from "@/lib/api";
import type { ImprovementSummary } from "@/lib/schemas";
import { formatDate } from "@/lib/utils";

const labels: Record<ImprovementSummary["status"], string> = {
  preparing: "Preparando proposta", prepared: "Aguardando validação",
  validating: "Validação em execução", validation_failed: "Validação falhou",
  pending_review: "Aguardando revisão humana", no_changes: "Sem alterações aplicáveis",
  failed: "Preparação falhou", rejected: "Rejeitada pelo operador",
  committing: "Finalização do commit pendente", committed: "Commit aprovado e criado",
};

export default function ImprovementsPage() {
  const [token, setToken] = useState("");
  const [records, setRecords] = useState<ImprovementSummary[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestVersion = useRef(0);

  async function refresh() {
    const version = ++requestVersion.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.improvements(token);
      if (version === requestVersion.current) setRecords(result);
    } catch (cause) {
      if (version === requestVersion.current) {
        setRecords(null);
        setError(cause instanceof Error ? cause.message : "Falha ao consultar propostas.");
      }
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  }

  function clearSession() {
    requestVersion.current++;
    setToken("");
    setRecords(null);
    setError(null);
    setLoading(false);
  }

  return (
    <>
      <PageHeader eyebrow="Admin · Self improvement" title="Mudanças sob revisão."
        description="Acompanhe o caminho da evidência ao commit. Cada alteração depende da revisão explícita de uma pessoa."
        actions={<Badge tone="neutral"><LockKeyhole size={12} /> Acesso protegido</Badge>} />
      <Panel>
        <PanelHeader><PanelTitle>Acesso administrativo</PanelTitle></PanelHeader>
        <PanelContent>
          <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); void refresh(); }}>
            <label className="flex min-w-0 flex-1 flex-col gap-2 text-xs text-muted">
              Token administrativo
              <input type="password" autoComplete="off" value={token} required
                onChange={(event) => { clearSession(); setToken(event.target.value); }}
                className="h-10 w-full border border-line bg-ink px-3 text-foreground outline-offset-2 focus:outline-signal" />
            </label>
            <Button type="submit" disabled={loading || !token}><RefreshCw size={14} />{loading ? "Consultando…" : "Atualizar propostas"}</Button>
            <Button type="button" variant="outline" onClick={clearSession}>Limpar sessão</Button>
          </form>
          <p className="mt-3 text-xs text-dim">O token permanece apenas na memória desta página. A aprovação acontece no terminal do operador.</p>
          {error && <p className="mt-4 text-sm text-danger" role="alert">{error}</p>}
        </PanelContent>
      </Panel>

      {records !== null && <>
        <section className="my-6 grid gap-px border border-line bg-line sm:grid-cols-3" aria-label="Resumo de melhorias">
          {[
            ["Propostas", records.length],
            ["Revisão pendente", records.filter((item) => item.status === "pending_review").length],
            ["Commits aprovados", records.filter((item) => item.status === "committed").length],
          ].map(([label, count]) => <div key={label} className="bg-panel p-5"><p className="text-xs text-muted">{label}</p><p className="mt-2 font-mono text-3xl">{count}</p></div>)}
        </section>
        {records.length === 0 && <Panel><PanelContent className="py-8"><p>Nenhuma proposta registrada.</p><p className="mt-2 text-sm text-muted">O operador pode preparar uma proposta a partir de uma avaliação externa concluída e elegível. Avaliações inválidas e smoke fake não sustentam melhorias.</p></PanelContent></Panel>}
        <div className="space-y-5">{records.map((record) => <Panel key={record.proposal_id}>
          <PanelHeader className="flex flex-wrap items-center justify-between gap-3">
            <PanelTitle><GitCommitHorizontal className="mr-2 inline" size={16} />Proposta {record.proposal_id.slice(0, 8)}</PanelTitle>
            <Badge tone={record.status === "committed" ? "good" : record.error_code ? "danger" : "neutral"}>{labels[record.status]}</Badge>
          </PanelHeader>
          <PanelContent>
            <dl className="grid gap-4 text-xs sm:grid-cols-2">
              <Field label="Avaliação de origem" value={record.evaluation_id} />
              <Field label="Última atualização" value={formatDate(record.updated_at)} />
              <Field label="Branch local" value={record.branch} />
              <Field label="Commit base" value={record.base_commit} />
              <Field label="Validação local" value={record.validation_passed ? "Passou · não comprova ganho de qualidade" : "Sem validação aprovada"} />
              <Field label="SHA-256 do diff" value={record.patch_digest ?? "Sem patch"} />
              <Field label="Aprovação registrada" value={record.approved_by ?? "Sem aprovação"} />
              <Field label="Commit resultante" value={record.commit_sha ?? "Não criado"} />
            </dl>
            {record.changed_files.length > 0 && <div className="mt-5 border-t border-line pt-4"><p className="text-xs text-muted">Arquivos propostos</p><ul className="mt-2 space-y-1 font-mono text-xs">{record.changed_files.map((file) => <li className="break-all" key={file}>{file}</li>)}</ul></div>}
            {record.error_code && <p className="mt-4 font-mono text-xs text-danger">{record.error_code}</p>}
            <p className="mt-5 text-xs leading-5 text-muted">{nextStep(record)}</p>
          </PanelContent>
        </Panel>)}</div>
      </>}
      <p className="mt-6 text-xs leading-5 text-dim">Fluxo: proposta → validação local → revisão humana → commit local. O agente não abre PR, não faz push e não publica mudanças. Uma nova avaliação externa exige novo preflight e consentimento.</p>
    </>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0"><dt className="text-dim">{label}</dt><dd className="mt-1 break-all font-mono text-muted">{value}</dd></div>;
}

function nextStep(record: ImprovementSummary) {
  if (record.status === "prepared" || record.status === "validation_failed" || record.status === "validating") return `Operador: indusguard-eval improvement-validate ${record.proposal_id}`;
  if (record.status === "pending_review") return `Revise o diff no terminal: indusguard-eval improvement-review ${record.proposal_id}`;
  if (record.status === "committing") return `Recupere a finalização: indusguard-eval improvement-recover ${record.proposal_id}`;
  if (record.status === "committed") return "Commit criado na branch isolada. A incorporação e a avaliação posterior são decisões separadas do operador.";
  if (record.status === "no_changes") return "A receita já está aplicada ou não há receita elegível. Nenhum commit será criado.";
  return "Consulte os artefatos locais para revisar a proposta e seu estado.";
}
