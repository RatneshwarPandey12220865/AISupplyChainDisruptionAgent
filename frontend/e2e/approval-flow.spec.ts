import { expect, test } from "@playwright/test";

// Backend must run with API_KEYS containing:  alice:<E2E_APPROVER_KEY>:approver  and  vic:<E2E_VIEWER_KEY>:viewer
const APPROVER = process.env.E2E_APPROVER_KEY ?? "e2e-approver-key-0123456789";
const VIEWER = process.env.E2E_VIEWER_KEY ?? "e2e-viewer-key-0123456789";

async function signIn(page: import("@playwright/test").Page, key: string) {
  await page.goto("/");
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Supply Chain Control Tower" })).toBeVisible();
}

test("rejects a bad key, then an approver injects a disruption and approves the plan", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("API key").fill("definitely-wrong");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText(/invalid API key/i)).toBeVisible();

  await page.getByLabel("API key").fill(APPROVER);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("alice · approver")).toBeVisible();

  await page.getByRole("button", { name: "Vessel rollover (+10d)" }).click();
  // the live stream (or fallback poll) should surface the disruption without a manual reload
  const row = page.getByRole("link", { name: /Shipment Delay/ }).first();
  await expect(row).toBeVisible({ timeout: 60_000 });
  await row.click();

  await expect(page.getByText("Human approval required")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("Deciding as")).toContainText("alice");
  await page.getByRole("button", { name: /Approve & execute/ }).click();
  await expect(page.getByText(/approved executed/i).first()).toBeVisible({ timeout: 30_000 });
});

test("a viewer can look but not approve", async ({ page }) => {
  await signIn(page, VIEWER);
  await expect(page.getByRole("button", { name: "Vessel rollover (+10d)" })).toHaveCount(0);
  await expect(page.getByText("viewer", { exact: false }).first()).toBeVisible();
});
