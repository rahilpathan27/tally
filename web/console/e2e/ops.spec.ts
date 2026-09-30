import { expect, test } from "@playwright/test";
import { checkout, login } from "./helpers";

test("risk analyst approves a payment held for review", async ({ page, browser }) => {
  const paymentId = await checkout(page, "45,000.00", "meera@bank-a");
  await expect(page.getByTestId("checkout-status")).toHaveText("risk review");

  const analyst = await (await browser.newContext()).newPage();
  await login(analyst, "risk@tally.test");
  await analyst.goto("/ops/risk");
  await analyst.getByRole("button", { name: `Open case for payment ${paymentId}` }).click();
  await expect(analyst.locator("strong", { hasText: "LARGE_UPI_TRANSFER" })).toBeVisible();
  await analyst.getByLabel("Analyst note").fill("Customer confirmed by phone");
  await analyst.getByRole("button", { name: "Approve payment" }).click();
  await expect(analyst.getByRole("button", { name: `Open case for payment ${paymentId}` })).toHaveCount(0);

  // The checkout polls and settles once the analyst decides.
  await expect(page.getByTestId("checkout-status")).toHaveText("succeeded", { timeout: 15_000 });
});

test("a reconciliation adjustment needs a second person", async ({ page }) => {
  await login(page, "ops@tally.test");
  await page.goto("/ops/recon");
  await page.getByRole("table", { name: "Reconciliation breaks" }).getByRole("link").first().click();
  await expect(page.getByRole("heading", { name: "Three-way view" })).toBeVisible();
  const breakUrl = page.url();
  await page.getByLabel("Adjustment").selectOption("book_to_suspense");
  await page.getByLabel("Justification").fill("Unattributed credit; parked in suspense pending bank trace");
  await page.getByRole("button", { name: "Submit for approval" }).click();
  await expect(page.getByText("pending approval").first()).toBeVisible();

  await login(page, "approver@tally.test");
  await page.goto("/ops/approvals");
  const request = page.getByRole("listitem").filter({ hasText: "recon adjustment" }).filter({ hasText: "ops@tally.test" }).first();
  await request.getByLabel("Reason").fill("Matches bank trace ticket");
  await request.getByRole("button", { name: "Approve" }).click();
  await expect(request).toHaveCount(0);
  await page.goto(breakUrl);
  await expect(page.getByText("resolved").first()).toBeVisible();
  await expect(page.getByText("approved and executed")).toBeVisible();
});

test("switch monitor streams live updates", async ({ page }) => {
  await login(page, "ops@tally.test");
  await page.goto("/ops");
  await expect(page.getByText("Live (SSE)")).toBeVisible({ timeout: 10_000 });
});
