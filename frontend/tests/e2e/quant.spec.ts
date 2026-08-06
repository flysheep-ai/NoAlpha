import { expect, test } from "@playwright/test";

// Phase Q: the /quant page must render REAL registry + experiment content
// (populated by experiments/quant_phase_q.py & train_champion.py), and the
// API must serve the quant endpoints.

test("quant API serves real registry and experiments", async ({ request }) => {
  const overview = await (await request.get("http://127.0.0.1:8000/api/quant/overview")).json();
  console.log(`[AUDIT] quant overview: ${JSON.stringify(overview)}`);
  expect(overview.feature_count).toBeGreaterThan(20);
  expect(overview.models_total).toBeGreaterThan(0);

  const experiments = await (await request.get("http://127.0.0.1:8000/api/quant/experiments")).json();
  expect(experiments.experiments.length).toBeGreaterThan(0);
  const withSignal = experiments.experiments.filter((e: any) => e.signal?.rank_ic != null);
  expect(withSignal.length).toBeGreaterThan(0);
  console.log(`[AUDIT] experiments with IC: ${withSignal.length}`);

  const results = await (await request.get("http://127.0.0.1:8000/api/quant/results/cn")).json();
  expect(Object.keys(results.factor_ic).length).toBeGreaterThan(20);
  expect(results.hybrid_comparison.quant_only).toBeTruthy();
});

test("quant page renders models, experiments, factors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto("/quant");
  await expect(page.getByTestId("quant-page")).toBeVisible({ timeout: 15_000 });

  // active model table shows the promoted CN lightgbm
  await expect(page.getByTestId("quant-active-models")).toContainText(/lightgbm v1/, { timeout: 15_000 });

  // walk-forward experiment table has rows with net returns
  await expect(page.getByTestId("quant-experiments").locator("tbody tr").first()).toBeVisible();
  const expRows = await page.getByTestId("quant-experiments").locator("tbody tr").count();
  console.log(`[AUDIT] experiment rows: ${expRows}`);
  expect(expRows).toBeGreaterThan(3);

  // factor explorer renders and the US switch works
  await expect(page.getByTestId("quant-factors").locator("tbody tr").first()).toBeVisible();
  await page.getByRole("button", { name: "US" }).click();
  await expect(page.getByTestId("quant-factors").locator("tbody tr").first()).toBeVisible();

  // hybrid comparison shows honest losers too
  await expect(page.getByTestId("quant-hybrid")).toContainText("research_only_deterministic");

  // feature registry lists versioned features with formulas
  await expect(page.getByTestId("quant-features")).toContainText("return_20d:v1");

  console.log(`[AUDIT] console errors: ${JSON.stringify(errors)}`);
  expect(errors).toEqual([]);
});
