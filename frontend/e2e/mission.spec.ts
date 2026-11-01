import { expect, test } from "@playwright/test";

test("administrator can open mission control", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("kubeml-local");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Workspaces" })).toBeVisible();
  await expect(page.getByText("Inference")).toBeVisible();
  await page.getByRole("button", { name: "Light" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByRole("link", { name: "Policy" }).click();
  await expect(page.getByText("does not authorise an automatic rollback")).toBeVisible();
});
