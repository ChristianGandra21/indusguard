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
  await expect(page.getByText("Este resultado não sustenta a hipótese.")).toBeVisible();
  await page.getByRole("link", { name: /abrir/ }).first().click();

  await expect(page.getByRole("heading", { name: "Veja o caminho. Não o conteúdo." })).toBeVisible();
  await expect(page.getByText("synthetic__getWidget")).toBeVisible();
  await expect(page.getByText("READ_APPROVED")).toBeVisible();
  await expect(page.getByText("conteúdo sintético omitido do dashboard")).not.toBeVisible();
});
