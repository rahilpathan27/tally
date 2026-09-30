import { expect, test } from "@playwright/test";
import { checkout, login } from "./helpers";

test("merchant refunds part of a payment and cannot over-refund", async ({ page }) => {
  const paymentId = await checkout(page, "20.00", "asha@bank-a");
  await login(page, "admin@demo.test");
  await page.goto(`/merchant/payments/${paymentId}`);
  await expect(page.getByRole("heading", { name: "₹20.00" })).toBeVisible();
  await page.getByRole("button", { name: "Refund" }).click();
  const dialog = page.getByRole("dialog", { name: "Refund payment" });
  await dialog.getByLabel("Amount (₹)").fill("25");
  await dialog.getByRole("button", { name: "Refund" }).click();
  await expect(dialog.getByRole("alert")).toContainText("At most ₹20.00");
  await dialog.getByLabel("Amount (₹)").fill("7.50");
  await dialog.getByRole("button", { name: "Refund" }).click();
  await expect(dialog.getByRole("status")).toContainText("Refund created");
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("table", { name: "Refunds" })).toContainText("₹7.50");
});

test("viewer role has no refund or developer access", async ({ page }) => {
  await login(page, "viewer@demo.test");
  await expect(page.getByRole("link", { name: "Developers" })).toHaveCount(0);
  await page.goto("/merchant/payments");
  await page.getByRole("table", { name: "Payments" }).getByRole("link").first().click();
  await expect(page.getByText("Status timeline")).toBeVisible();
  await expect(page.getByRole("button", { name: "Refund" })).toHaveCount(0);
});

test("a new API key's secret is shown once", async ({ page }) => {
  await login(page, "dev@demo.test");
  await page.goto("/merchant/developers");
  await page.getByRole("button", { name: "Create key" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Create" }).click();
  await expect(page.getByTestId("one-time-secret")).toContainText("tly_test_");
  await page.getByRole("button", { name: "I have stored it" }).click();
  await page.reload();
  await expect(page.getByTestId("one-time-secret")).toHaveCount(0);
});
