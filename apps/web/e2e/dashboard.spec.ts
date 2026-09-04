import { expect, test } from "@playwright/test";

test("navega pelo dashboard seguro até um trace público", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "A camada segura entre intenção e ação." })).toBeVisible();
  await expect(page.getByText("API operacional")).toBeVisible();

  await page.getByRole("link", { name: /Conectores/ }).click();
  await expect(page.getByRole("heading", { name: "Capacidades declaradas. Permissões explícitas." })).toBeVisible();
  await expect(page.getByText("Tractian Industrial Support")).toBeVisible();

  await page.getByRole("link", { name: /Avaliações/ }).click();
  await expect(page.getByText("smoke offline", { exact: true })).toBeVisible();
  await expect(page.getByText("Smoke de infraestrutura")).toBeVisible();
  await expect(page.getByText("Próxima ação segura")).toBeVisible();
  await page.getByRole("link", { name: /abrir/ }).first().click();

  await expect(page.getByRole("heading", { name: "Veja o caminho. Não o conteúdo." })).toBeVisible();
  await expect(page.getByText("synthetic__getWidget")).toBeVisible();
  await expect(page.getByText("READ_APPROVED", { exact: true })).toBeVisible();
  await expect(page.getByText("conteúdo sintético omitido do dashboard")).not.toBeVisible();

  await page.getByRole("link", { name: /Playground/ }).click();
  await expect(page.getByRole("heading", { name: "Teste o agente. Preserve a fronteira." })).toBeVisible();
  await page.getByLabel("Token do proprietário").fill(
    "e2e-owner-token-with-at-least-thirty-two-chars",
  );
  await page.getByRole("button", { name: "Salvar acesso nesta sessão" }).click();
  await page.getByLabel("widget_id").selectOption("widget-1");
  await page.getByLabel("Solicitação").fill("Qual é o estado do widget widget-1?");
  await page.getByRole("button", { name: "Executar agente protegido" }).click();

  await expect(page.getByText("O widget está ativo [ev-001].")).toBeVisible();
  await expect(page.getByText("synthetic__getWidget")).toBeVisible();
  await expect(page.getByText("READ_APPROVED", { exact: true })).toBeVisible();
  await expect(page.getByText("e2e-owner-token-with-at-least-thirty-two-chars")).not.toBeVisible();

  await page.getByRole("link", { name: "Ver trace" }).click();
  await expect(page.getByRole("heading", { name: "Veja o caminho. Não o conteúdo." })).toBeVisible();
  await expect(page.getByLabel("Runs recentes")).toBeEnabled();
  await expect(page.getByLabel("ID da run")).not.toHaveValue("");
  await expect(page.getByText("synthetic__getWidget")).toBeVisible();
});
