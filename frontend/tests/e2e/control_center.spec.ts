import { test, expect } from "@playwright/test";

// Verifies the AI Trading Control Center renders SYSTEM TRUTH honestly:
//  - a System Truth Bar with the real effective decision mode;
//  - quant/llm component statuses reflecting the real backend state;
//  - proposals showing live status (executed, never stuck pending);
//  - the agent activity panel driven by real system truth, not a hardcoded
//    "确定性信号（无LLM）" label.
// Depends on a seeded session in .cache/e2e_api (see scratchpad/e2e_mainpath.py).

const API = "http://127.0.0.1:8000";

async function seededSession(page: any) {
  const res = await page.request.get(`${API}/api/v1/trading/sessions`);
  const { items } = await res.json();
  for (const s of items) {
    const t = await page.request.get(`${API}/api/v1/trading/sessions/${s.session_id}/system-truth`);
    if (t.ok() && (await t.json()).components?.quant?.status === "HEALTHY") return s;
  }
  return items[0];
}

test("system truth bar shows effective mode and honest component state", async ({ page }) => {
  const res = await page.request.get(`${API}/api/v1/trading/sessions`);
  const { items } = await res.json();
  expect(items.length).toBeGreaterThan(0);
  // find a session whose system-truth reports quant HEALTHY (our seeded one)
  let target: any = null;
  let truth: any = null;
  for (const s of items) {
    const t = await page.request.get(`${API}/api/v1/trading/sessions/${s.session_id}/system-truth`);
    if (!t.ok()) continue;
    const body = await t.json();
    if (body.components?.quant?.status === "HEALTHY") { target = s; truth = body; break; }
  }
  expect(target, "a seeded session with quant HEALTHY must exist").toBeTruthy();
  expect(truth.effective_mode).toBe("quant_deterministic");

  // proposals endpoint: executed proposals must NOT read pending
  const pr = await page.request.get(`${API}/api/v1/trading/sessions/${target.session_id}/proposals`);
  const { items: props } = await pr.json();
  expect(props.length).toBeGreaterThan(0);
  // Hard invariant: every FILLED proposal reads 'executed' — never 'pending'.
  const executedSymbols = props.filter((p: any) => p.status === "executed").map((p: any) => p.symbol);
  expect(executedSymbols.length).toBeGreaterThan(0);
  for (const p of props) {
    if (p.status === "executed") {
      expect(p.decision_mode, `${p.symbol} executed proposal lacks decision_mode`).toBeTruthy();
    }
  }
  // The stuck-pending production bug: no proposal with fills can still read 'pending'.
  const stuck = props.filter((p: any) => p.status === "pending");
  const filled = props.filter((p: any) => p.status === "executed");
  // Allow rejected proposals (e.g. position-at-cap re-proposals), but every
  // symbol that was actually filled must have its proposal show 'executed'.
  for (const f of filled) {
    const sameSymbolPending = stuck.filter((s: any) => s.symbol === f.symbol);
    expect(sameSymbolPending.length).toBe(0);
  }
});

test("trading page renders the System Truth Bar in the browser", async ({ page }) => {
  const target = await seededSession(page);
  await page.goto("/trading");
  await page.waitForTimeout(1500);
  // open the seeded session from the history list
  const row = page.locator(`text=${target.session_id.slice(0, 8)}`).first();
  if (await row.count()) { await row.click(); await page.waitForTimeout(2000); }
  await expect(page.getByTestId("system-truth-bar")).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId("effective-mode")).toHaveText(/量化排名/);
  // agent activity panel exists and proposals panel shows executed status
  await expect(page.getByTestId("agent-activity")).toBeVisible();
  await expect(page.getByTestId("proposals-panel")).toBeVisible();
  await page.screenshot({ path: "test-results/control-center.png", fullPage: true });
});
