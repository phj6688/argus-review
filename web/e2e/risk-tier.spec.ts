import { expect, test } from "@playwright/test";

test("the reviews list shows path and size categories and leaves old rows blank", async ({ page }, info) => {
  await page.goto("/reviews", { waitUntil: "networkidle" });

  const chips = page.getByTestId("risk-tier");
  await expect(chips).toHaveCount(11);
  await expect(chips.filter({ hasText: /^safe$/ })).toHaveCount(9);
  await expect(chips.filter({ hasText: /^risky$/ })).toHaveCount(2);
  await expect(page.locator('[data-testid="risk-tier"][data-tier="safe"]')).toHaveCount(9);
  await expect(page.locator('[data-testid="risk-tier"][data-tier="risky"]')).toHaveCount(2);

  const infra = page.getByRole("link").filter({ hasText: "Add auth rate-limit cache" });
  await expect(infra.getByTestId("risk-tier")).toHaveAttribute(
    "data-tip",
    /not a correctness verdict.*deploy\/docker-compose\.prod\.yml/,
  );
  const oldRow = page.getByRole("link").filter({ hasText: "editor: lazy-load language servers" });
  await expect(oldRow.getByTestId("risk-tier")).toHaveCount(0);

  for (const chip of await chips.all()) {
    await expect(chip).not.toContainText(/clean|approved|no defect/i);
    await expect(chip).not.toHaveAttribute("data-tip", /clean|approved|no defect/i);
    if (info.project.name === "mobile") await expect(chip).toBeHidden();
    else await expect(chip).toBeVisible();
  }

  if (info.project.name === "desktop") {
    const table = page.getByRole("region", { name: "Reviews table" });
    expect(await table.evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(1280);
    const overflowingColumns = await page.locator('a[href^="/reviews/"]').evaluateAll((rows) =>
      rows.flatMap((row) => Array.from(row.children).flatMap((column, index) => {
        if (index !== 2 && index !== 3) return [];
        const right = column.getBoundingClientRect().right;
        return Array.from(column.children)
          .filter((content) => content.getBoundingClientRect().right > right + 1)
          .map((content) => content.textContent);
      })),
    );
    expect(overflowingColumns).toEqual([]);
    await page.screenshot({ path: "e2e/screenshots/risk-tier-list.png", fullPage: true });
  }
});

test("review detail explains the category without giving a correctness verdict", async ({ page }) => {
  await page.goto("/reviews/acme-web-142");

  const note = page.getByTestId("risk-note");
  await expect(note).toBeVisible();
  await expect(note).toContainText("Risk tier: safe (path and size rule only; not a correctness verdict)");
  await expect(note).toContainText("59 changed lines");
  await expect(note).toContainText("300 line limit");
  await expect(note).not.toContainText(/clean|approved|no defect/i);
});

for (const id of ["acme-web-bare", "acme-web-error"]) {
  test(`${id} has no risk note`, async ({ page }) => {
    await page.goto(`/reviews/${id}`);
    await expect(page.getByTestId("risk-note")).toHaveCount(0);
    await expect(page.getByText("-", { exact: true })).toHaveCount(0);
  });
}

test("the overview feed keeps its existing columns", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Fleet health").first()).toBeVisible();
  await expect(page.getByTestId("risk-tier")).toHaveCount(0);
});

test("tablet users can scroll to every review column without widening the page", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  await page.goto("/reviews", { waitUntil: "networkidle" });

  const table = page.getByRole("region", { name: "Reviews table" });
  await expect(table).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(1024);
  expect(await table.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);

  const firstRow = table.locator('a[href="/reviews/acme-infra-218"]');
  await expect(firstRow).toBeVisible();
  const titleWidth = await firstRow.locator(":scope > div").nth(1).evaluate((element) => element.clientWidth);
  expect(titleWidth).toBeGreaterThanOrEqual(90);

  await table.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => table.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  await table.hover();
  await page.mouse.wheel(1000, 0);
  await expect.poll(() => table.evaluate((element) => element.scrollWidth - element.clientWidth - element.scrollLeft)).toBe(0);
  for (const index of [3, 4, 5, 6]) {
    const column = firstRow.locator(":scope > div").nth(index);
    await expect(column).toBeVisible();
    const bounds = await column.boundingBox();
    const tableBounds = await table.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(tableBounds!.x);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(tableBounds!.x + tableBounds!.width);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(1024);
});
