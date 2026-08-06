import { expect, test } from "@playwright/test";

test("runs AAPL investigation and opens claim evidence", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });

  await page.goto("/investigate");
  await page.getByLabel("Symbol").fill("AAPL");
  await page.getByLabel("Lookback Days").fill("60");
  await page.getByRole("button", { name: "Start Investigation" }).click();

  await expect(page.getByText(/Status:/)).toBeVisible();
  await page.waitForURL(/\/investigations\/.+/, { timeout: 120_000 });
  await expect(page.getByText("Executive Summary")).toBeVisible();
  await expect(page.getByText("Confidence Breakdown")).toBeVisible();
  await expect(page.getByText(/observations/i)).toBeVisible();

  const firstClaim = page.locator(".claim-card").first();
  await expect(firstClaim).toBeVisible();
  await firstClaim.click();
  await expect(page.getByText("Supporting Evidence")).toBeVisible();
  await expect(page.locator(".evidence-card").first()).toBeVisible();

  await page.getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "quality" }).click();
  await expect(page.getByText("Raw Evidence")).toBeVisible();
  await expect(page.getByText("Duplicates Removed")).toBeVisible();

  await page.goto("/providers");
  await expect(page.getByText("Provider Health")).toBeVisible();
  await expect(page.getByText("SEC EDGAR")).toBeVisible();
  await expect(page.getByText("Credential Required")).toBeVisible();

  expect(errors).toEqual([]);
});
