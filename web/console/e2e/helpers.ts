import { type Page, expect } from "@playwright/test";

export const PASSWORD = "correct horse battery staple";

export async function login(page: Page, email: string) {
  await page.context().clearCookies();
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Console" })).toContainText(email);
}

export async function checkout(page: Page, amount: string, payer: string): Promise<string> {
  await page.goto("/checkout");
  await page.getByLabel("Amount (₹)").fill(amount);
  await page.getByRole("radio", { name: "UPI" }).check();
  await page.getByLabel("Your UPI ID").selectOption(payer);
  await page.getByRole("button", { name: /^Pay/ }).click();
  await expect(page.getByTestId("checkout-status")).toBeVisible();
  const text = await page.getByText(/^Payment [0-9a-f-]{36}$/).textContent();
  return (text ?? "").replace("Payment ", "").trim();
}
