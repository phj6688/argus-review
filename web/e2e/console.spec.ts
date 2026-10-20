import { expect, test } from "@playwright/test";

/* Smoke + screenshot every console screen. Asserts each renders its key
   heading (catches a blank/errored screen) and saves a full-page shot. */

const SCREENS: [name: string, route: string, marker: string][] = [
  ["overview", "/", "Fleet health"],
  ["reviews", "/reviews", "All reviewed pull requests"],
  ["pr-detail", "/reviews/acme-web-142", "Bug-class findings"],
  ["reports", "/reports", "The action gap"],
  ["benchmark", "/benchmark", "Argus vs CodeRabbit"],
  ["config", "/config", "Repo allowlist"],
  ["integrations", "/integrations", "MCP server"],
];

for (const [name, route, marker] of SCREENS) {
  test(`${name} renders`, async ({ page }, info) => {
    await page.goto(route, { waitUntil: "networkidle" });
    await expect(page.getByText(marker, { exact: false }).first()).toBeVisible();
    await page.screenshot({
      path: `e2e/screenshots/${name}-${info.project.name}.png`,
      fullPage: true,
    });
  });
}

test("rail navigates between screens", async ({ page }) => {
  await page.goto("/", { waitUntil: "networkidle" });
  const reviews = page.getByRole("link", { name: "Reviews" }).first();
  await reviews.click();
  await expect(page).toHaveURL(/\/reviews$/);
});

test("review row opens the PR detail", async ({ page }) => {
  await page.goto("/reviews", { waitUntil: "networkidle" });
  await page.getByText("Bind metrics endpoint to loopback only").first().click();
  await expect(page).toHaveURL(/\/reviews\/acme-web-142$/);
  await expect(page.getByText("Bug-class findings")).toBeVisible();
});

test("config model picker lists env-provided options", async ({ page }) => {
  await page.goto("/config", { waitUntil: "networkidle" });
  const picker = page.getByLabel("Reviewer model");
  await expect(picker).toBeVisible();
  const options = await picker.locator("option").allTextContents();
  expect(options.length).toBeGreaterThanOrEqual(2);
  await picker.selectOption({ index: 1 });
  await expect(page.getByRole("button", { name: /Save changes/i })).toBeEnabled();
});

test("integrations endpoint paths render in full", async ({ page }) => {
  await page.goto("/integrations", { waitUntil: "networkidle" });
  for (const path of ["/benchmark", "/integrations", "/reports/{period}", "/findings/{finding_id}/label"]) {
    const el = page.getByText(path, { exact: true }).first();
    await expect(el).toBeVisible();
    const clipped = await el.evaluate((n) => n.scrollWidth > n.clientWidth);
    expect(clipped).toBe(false);
  }
});
