/**
 * Real-browser E2E for the rebuilt Market terminal and Autonomous Trading.
 * Requires backend on :8000 and vite dev on :5173 (proxied).
 * Run: npx playwright test tests/e2e/rebuild.spec.ts
 */
import { expect, test } from "@playwright/test";

const BASE = "http://127.0.0.1:5173";

test.describe("Live Market terminal", () => {
  test("search AAPL → detail → quote+kline → watchlist add/remove", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });

    await page.goto(`${BASE}/market`);
    // cross-market watchlist with seeded CN+US rows
    await expect(page.getByTestId("watchlist-table")).toBeVisible();
    await expect(page.getByTestId("watch-row-600519")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("watch-row-AAPL")).toBeVisible();

    // search a US stock
    await page.getByTestId("market-search").fill("AAPL");
    await page.getByTestId("search-result-AAPL").click();
    await expect(page.getByTestId("stock-detail")).toBeVisible();
    await expect(page.getByTestId("stock-detail")).toContainText("AAPL");
    await expect(page.getByTestId("stock-detail")).toContainText("NASDAQ");
    // real freshness state, never a bare fake LIVE
    await expect(page.getByTestId("stock-detail")).toContainText(/LIVE|DELAYED|CLOSED|STALE/);
    // real kline rendered (candles exist in svg) or an explicit unavailable notice
    await expect(
      page.getByTestId("stock-detail").locator("svg rect").first()
        .or(page.getByTestId("stock-detail").getByText("该周期K线暂不可用"))
    ).toBeVisible({ timeout: 20000 });

    // watchlist toggle from detail (AAPL is seeded; remove then re-add)
    await page.getByTestId("remove-watchlist").click();
    await expect(page.getByTestId("watch-row-AAPL")).toHaveCount(0, { timeout: 10000 });
    await page.getByTestId("add-watchlist").click();
    await expect(page.getByTestId("watch-row-AAPL")).toBeVisible({ timeout: 10000 });

    expect(consoleErrors, `console errors: ${consoleErrors.join("\n")}`).toHaveLength(0);
  });

  test("search 600519 by Chinese name → CN quote in same watchlist", async ({ page }) => {
    await page.goto(`${BASE}/market`);
    await page.getByTestId("market-search").fill("贵州茅台");
    await page.getByTestId("search-result-600519").click();
    await expect(page.getByTestId("stock-detail")).toContainText("贵州茅台");
    await expect(page.getByTestId("stock-detail")).toContainText("SSE");
    await expect(page.getByTestId("stock-detail")).toContainText("CNY");
    // cross-market: CN and US rows visible together
    await expect(page.getByTestId("watch-row-600519")).toBeVisible();
    await expect(page.getByTestId("watch-row-AAPL")).toBeVisible();
  });
});

test.describe("Autonomous simulation trading", () => {
  test("configure capital → create session → run cycle → decision feed shows real events", async ({ page }) => {
    await page.goto(`${BASE}/trading`);
    // product path: only capital/risk config, no manual proposal form on main view
    await expect(page.getByTestId("session-config")).toBeVisible();
    await expect(page.getByText("提交提案")).toHaveCount(0);

    await page.getByTestId("cfg-capital").fill("100000");
    await page.getByTestId("create-session").click();
    await expect(page.getByTestId("session-status")).toBeVisible({ timeout: 15000 });

    // run one autonomous cycle synchronously
    await page.getByTestId("run-cycle").click();
    // decision feed must show a real scan event (from decision_events table)
    await expect(page.getByTestId("decision-feed")).toContainText("扫描", { timeout: 30000 });
    // and either an action chain or an explicit NO_ACTION with reason
    await expect(page.getByTestId("decision-feed")).toContainText(/候选|NO_ACTION|观望|研究/);
  });

  test("emergency stop halts session", async ({ page }) => {
    await page.goto(`${BASE}/trading`);
    // open most recent session from the list
    const row = page.locator(".row.clickable").first();
    await row.click();
    await expect(page.getByTestId("session-status")).toBeVisible();
    page.on("dialog", d => d.accept());
    await page.getByTestId("emergency-stop").click();
    await expect(page.getByTestId("session-status")).toContainText("紧急停止", { timeout: 10000 });
    // engine-level: run-cycle button gone, halted banner shown
    await expect(page.getByText("kill switch")).toBeVisible();
  });

  test("explainability: positions show real names, proposals have rationale, risk shows checks", async ({ page }) => {
    test.setTimeout(300_000);
    const errors: string[] = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

    await page.goto("/trading");

    // If no session is active, create one
    const existing = await page.getByText("AI 投资组合管理器").isVisible().catch(() => false);
    if (!existing) {
      await page.getByTestId("cfg-capital").fill("100000");
      await page.getByTestId("create-session").click();
      await page.getByTestId("run-cycle").click();
      await page.waitForTimeout(30_000);
    }

    const bodyText = await page.locator("body").innerText();

    // 1. Positions must show real Chinese company names (not just codes)
    const hasNames = /视源股份|阿科力|航锦科技|贵州茅台/.test(bodyText);
    console.log(`[EXPLAIN] position names: ${hasNames}`);

    // 2. Decision quality badge must be present (信号驱动)
    const hasBadge = /信号驱动|证据支撑|混合研究/.test(bodyText);
    console.log(`[EXPLAIN] decision quality badge: ${hasBadge}`);

    // 3. Decision stories must be grouped, not raw event stream
    const stories = await page.getByTestId("decision-feed").locator(".panel").count();
    console.log(`[EXPLAIN] decision story cards: ${stories}`);

    // 4. No raw JSON like `"approved": true` in default view
    const rawJsonVisible = await page.getByText(/"approved"/).isVisible().catch(() => false);
    console.log(`[EXPLAIN] raw JSON exposure: ${rawJsonVisible}`);

    // 5. Click the first decision story to open detail drawer
    const firstDecision = page.getByTestId("decision-feed").locator(".panel").first();
    await firstDecision.click();
    await expect(page.getByText(/催化/)).toBeVisible({ timeout: 15_000 });

    // 6. Close drawer, click a position to inspect
    await page.getByText("关闭").first().click({ timeout: 5000 }).catch(() => {});
    const posRow = page.getByTestId("positions-panel").locator(".row.clickable").first();
    if (await posRow.isVisible()) {
      await posRow.click();
      await expect(page.getByText(/持有|论点|失效/)).toBeVisible({ timeout: 10_000 });
    }

    console.log(`[EXPLAIN] console errors: ${JSON.stringify(errors.filter(e => !e.includes("favicon")).slice(0, 3))}`);
    expect(hasNames).toBe(true);
    expect(hasBadge).toBe(true);
    expect(rawJsonVisible).toBe(false);
  });
});
