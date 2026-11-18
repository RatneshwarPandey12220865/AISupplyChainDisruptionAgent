import { expect, test } from "@playwright/test";

// Backend must run with API_KEYS containing:
//   alice:<E2E_APPROVER_KEY>:approver  vic:<E2E_VIEWER_KEY>:viewer  root:<E2E_ADMIN_KEY>:admin
const APPROVER = process.env.E2E_APPROVER_KEY ?? "e2e-approver-key-0123456789";
const VIEWER = process.env.E2E_VIEWER_KEY ?? "e2e-viewer-key-0123456789";
const ADMIN = process.env.E2E_ADMIN_KEY ?? "e2e-admin-key-0123456789";

type Page = import("@playwright/test").Page;

async function useKeyForm(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with an API key instead" }).click();
}

async function signInWithKey(page: Page, key: string) {
  await useKeyForm(page);
  await page.getByLabel("API key").fill(key);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Supply Chain Control Tower" })).toBeVisible();
}

test("rejects a bad key, then an approver injects a disruption and approves the plan", async ({ page }) => {
  await useKeyForm(page);
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
  await signInWithKey(page, VIEWER);
  await expect(page.getByRole("button", { name: "Vessel rollover (+10d)" })).toHaveCount(0);
  await expect(page.getByText("viewer", { exact: false }).first()).toBeVisible();
});

test("an admin bootstraps from an API key, creates an account, and that user signs in with a password", async ({ page }) => {
  const username = `e2e-${Date.now().toString(36)}`;
  const password = "e2e-correct-horse-battery";

  await signInWithKey(page, ADMIN);
  await page.getByRole("link", { name: "Users" }).click();
  await page.getByLabel("Username").fill(username);
  await page.getByLabel(/Initial password/).fill(password);
  await page.getByRole("button", { name: "Create user" }).click();
  await expect(page.getByText(`Created ${username}`)).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill("wrong-password-here");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Invalid username or password")).toBeVisible();

  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText(`${username} · approver`)).toBeVisible();
  await expect(page.getByRole("link", { name: "Users" })).toHaveCount(0); // approvers cannot manage users
  await page.goto("/users");
  await expect(page.getByText("Only administrators can manage users.")).toBeVisible();
  await page.getByRole("link", { name: "Control Tower" }).click();
  await page.getByRole("button", { name: "Vessel rollover (+10d)" }).click(); // approvers can inject

  // the session survives a reload
  await page.reload();
  await expect(page.getByText(`${username} · approver`)).toBeVisible();
});
