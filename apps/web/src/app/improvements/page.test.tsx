import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { ImprovementSummary } from "@/lib/schemas";

import ImprovementsPage from "./page";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const proposal: ImprovementSummary = {
  proposal_id: "11111111-1111-4111-8111-111111111111",
  evaluation_id: "22222222-2222-4222-8222-222222222222",
  status: "pending_review", created_at: "2026-09-04T12:00:00Z", updated_at: "2026-09-04T12:00:00Z",
  base_commit: "a".repeat(40), branch: "improvement/test", changed_files: ["connectors/tractian/domain.yaml"],
  patch_digest: "b".repeat(64), validation_passed: true, approved_by: null, approved_at: null,
  commit_sha: null, error_code: null,
};

describe("admin improvements", () => {
  it("loads only after token submission and clears sensitive state on logout", async () => {
    const fetch = vi.spyOn(api, "improvements").mockResolvedValue([proposal]);
    const user = userEvent.setup();
    render(<ImprovementsPage />);
    expect(fetch).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("Token administrativo"), "admin-token");
    await user.click(screen.getByRole("button", { name: "Atualizar propostas" }));
    expect(await screen.findByText("Aguardando revisão humana")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("admin-token");
    expect(screen.getByText(/improvement-review/)).toBeInTheDocument();
    expect(screen.getByText("Não criado")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /aprovar/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Limpar sessão" }));
    expect(screen.queryByText("Aguardando revisão humana")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Token administrativo")).toHaveValue("");
  });

  it("distinguishes empty history from an access error", async () => {
    const fetch = vi.spyOn(api, "improvements").mockResolvedValue([]);
    const user = userEvent.setup();
    render(<ImprovementsPage />);
    await user.type(screen.getByLabelText("Token administrativo"), "token");
    await user.click(screen.getByRole("button", { name: "Atualizar propostas" }));
    expect(await screen.findByText("Nenhuma proposta registrada.")).toBeInTheDocument();
    fetch.mockRejectedValue(new Error("Acesso administrativo necessário."));
    await user.click(screen.getByRole("button", { name: "Atualizar propostas" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Acesso administrativo necessário.");
    expect(screen.queryByText("Nenhuma proposta registrada.")).not.toBeInTheDocument();
  });
});
