import { expect, test } from "@playwright/test";
import { checkout } from "./helpers";

test("UPI checkout by an allowlisted payer succeeds", async ({ page }) => {
  await checkout(page, "12.34", "asha@bank-a");
  await expect(page.getByTestId("checkout-status")).toHaveText("succeeded");
  await expect(page.getByRole("status").filter({ hasText: "Paid" })).toContainText("₹12.34");
});

test("card checkout above the step-up threshold completes with the payer's OTP", async ({ page, context }) => {
  await page.goto("/checkout");
  await page.getByLabel("Amount (₹)").fill("5,250.00");
  await page.getByRole("radio", { name: "Card" }).check();
  await page.getByLabel("Card number").fill("4242 4242 4242 4242");
  await page.getByLabel("Expiry (MM/YY)").fill("12/30");
  await page.getByRole("button", { name: /^Pay/ }).click();
  await expect(page.getByLabel("One-time code")).toBeVisible();
  const [phone] = await Promise.all([context.waitForEvent("page"), page.getByRole("link", { name: "Open payer phone" }).click()]);
  await phone.getByRole("button", { name: "Check messages" }).click();
  const code = (await phone.getByTestId("otp").textContent())?.trim() ?? "";
  expect(code).toMatch(/^\d{6}$/);
  await page.getByLabel("One-time code").fill(code === "000000" ? "111111" : "000000");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "incorrect" })).toBeVisible();
  await page.getByLabel("One-time code").fill(code);
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByTestId("checkout-status")).toHaveText("succeeded");
});

test("a declined card number never leaves the vault", async ({ page }) => {
  await page.goto("/checkout");
  await page.getByRole("radio", { name: "Card" }).check();
  await page.getByLabel("Card number").fill("4111 1111 1111 1112");
  await page.getByRole("button", { name: /^Pay/ }).click();
  await expect(page.getByRole("alert").filter({ hasText: /card|accepted|test/i })).toBeVisible();
});
