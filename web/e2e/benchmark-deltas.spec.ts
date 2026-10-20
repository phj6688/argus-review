import { expect, test } from "@playwright/test";

/* Benchmark deltas: the guideline-citing row, the per-repo severity-tinted table,
   and (when the API reports a fresh DB) the Argus empty state. The default board
   uses the two-sided demo fixture; the empty state is asserted separately when
   NEXT_PUBLIC_ARGUS_API points at a fresh backend. */

test("benchmark shows guideline row and per-repo table", async ({ page }, info) => {
  await page.goto("/benchmark", { waitUntil: "networkidle" });

  // GAP 1: the new guideline-citing metric row.
  await expect(page.getByText("Cites guideline").first()).toBeVisible();
  await expect(page.getByText("grounded in docs").first()).toBeVisible();

  // The recall/FP rows carry the rebuilt gate's pending tag.
  await expect(page.getByText("THE REAL GATE", { exact: false }).first()).toBeVisible();

  // GAP 3: the per-repo breakdown table, severity-tinted crit+major column.
  await expect(page.getByText("CodeRabbit snapshot corpus", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("crit+major", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("acme/docs").first()).toBeVisible();
  await expect(page.getByText("acme/billing").first()).toBeVisible();

  await page.screenshot({
    path: `e2e/screenshots/benchmark-deltas-${info.project.name}.png`,
    fullPage: true,
  });
});

test("benchmark shows the Argus empty state when the API reports no runs", async ({
  page,
}, info) => {
  await page.goto("/benchmark", { waitUntil: "networkidle" });

  const empty = page.getByText("No Argus runs yet").first();
  if (await empty.count().then(() => empty.isVisible()).catch(() => false)) {
    await expect(empty).toBeVisible();
    await expect(page.getByText("no data").first()).toBeVisible();
    await page.screenshot({
      path: `e2e/screenshots/benchmark-empty-${info.project.name}.png`,
      fullPage: true,
    });
  } else {
    test.skip(true, "API not wired to a fresh DB; empty state covered by the live-backend run");
  }
});
