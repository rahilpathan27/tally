import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { login } from "./helpers";

async function audit(page: import("@playwright/test").Page, path: string) {
  await page.goto(path);
  await page.waitForLoadState("networkidle");
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
  expect(serious.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`), path).toEqual([]);
}

test("public pages have no serious accessibility violations", async ({ page }) => {
  for (const path of ["/", "/login", "/checkout", "/payer"]) await audit(page, path);
});

test("merchant pages have no serious accessibility violations", async ({ page }) => {
  await login(page, "admin@demo.test");
  for (const path of ["/merchant", "/merchant/payments", "/merchant/settlements", "/merchant/developers"]) {
    await audit(page, path);
  }
});

test("ops pages have no serious accessibility violations", async ({ page }) => {
  await login(page, "ops@tally.test");
  for (const path of ["/ops", "/ops/recon", "/ops/ledger", "/ops/approvals"]) await audit(page, path);
});

test("keyboard users can reach the main content and navigation", async ({ page }) => {
  await login(page, "admin@demo.test");
  await page.goto("/merchant");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
});
