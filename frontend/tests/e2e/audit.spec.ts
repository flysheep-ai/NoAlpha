import { expect, test } from "@playwright/test";

// Adversarial completion audit — collects runtime evidence.
// Logs findings with [AUDIT] prefix; hard-fails only on page-level breakage.

test.describe.configure({ mode: "serial" });

test("market page: live terminal reality check", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto("/market");
  await expect(page.getByText(/实时行情已连接|Live Market Connected/)).toBeVisible({ timeout: 15_000 });

  // Wait for quotes to render
  await page.waitForTimeout(5000);
  const rows = await page.locator(".data-table .row").count();
  console.log(`[AUDIT] watchlist rows rendered: ${rows}`);

  const liveBadges = await page.getByText("LIVE", { exact: true }).count();
  console.log(`[AUDIT] LIVE badges shown (market is CLOSED right now): ${liveBadges}`);

  const bodyText = await page.locator("body").innerText();
  console.log(`[AUDIT] US symbols present on market page: ${/AAPL|NVDA|MSFT/.test(bodyText)}`);
  console.log(`[AUDIT] console errors: ${JSON.stringify(errors)}`);
});

test("trading page: renders portfolio from backend", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const failed: string[] = [];
  page.on("requestfailed", (r) => failed.push(r.url()));

  await page.goto("/trading");
  await page.waitForTimeout(3000);
  const bodyText = await page.locator("body").innerText();
  console.log(`[AUDIT] trading page shows equity/cash: ${/94,?997|95,?78|100,?000/.test(bodyText)}`);
  console.log(`[AUDIT] trading page mentions kill switch: ${/kill|Kill|停止交易|STOP/.test(bodyText)}`);
  console.log(`[AUDIT] trading console errors: ${JSON.stringify(errors.slice(0, 5))}`);
  console.log(`[AUDIT] failed requests: ${JSON.stringify(failed.slice(0, 5))}`);
});

test("investigation: default llm mode falls back silently", async ({ page }) => {
  test.setTimeout(300_000);
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto("/investigate");
  await page.getByLabel(/股票代码|Symbol/).fill("AAPL");
  await page.getByRole("button", { name: /开始调查|Start Investigation/ }).click();

  await page.waitForURL(/\/investigations\/.+/, { timeout: 240_000 });
  await page.waitForTimeout(2000);
  const bodyText = await page.locator("body").innerText();
  console.log(`[AUDIT] report page shows llm mode label: ${/LLM|llm/.test(bodyText)}`);
  console.log(`[AUDIT] report page shows deepseek model pill: ${/deepseek/.test(bodyText)}`);
  console.log(`[AUDIT] report body is english-template despite zh ui: ${/investigation used \d+ financial facts/.test(bodyText)}`);
  console.log(`[AUDIT] investigation console errors: ${JSON.stringify(errors.slice(0, 5))}`);
});

test("chat: fresh session composer works; missing llm key surfaces guidance", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/chat");
  await page.getByRole("button", { name: /新对话|New Chat/ }).first().click();
  const input = page.locator(".chat-input-bar input");
  await expect(input).toBeVisible({ timeout: 10_000 });
  await expect(input).toBeEnabled({ timeout: 10_000 }); // regression: composer used to deadlock disabled

  await input.fill("今天大盘怎么看?");
  await input.press("Enter");

  // Without an LLM key the backend must persist a guidance message the user can see.
  await expect(page.locator(".chat-bubble.assistant").last()).toContainText(/LLM|API Key/, { timeout: 30_000 });
  // Composer must unlock again after the turn finishes.
  await expect(input).toBeEnabled({ timeout: 15_000 });
  console.log("[AUDIT] chat composer usable and llm-missing guidance rendered");
});

test("settings: llm configuration panel present", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByText(/LLM 模型|LLM Model/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/未配置|Not Configured/).first()).toBeVisible();
  console.log("[AUDIT] settings page renders LLM config panel");
});
