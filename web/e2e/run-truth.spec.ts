import { expect, test } from "@playwright/test";

/* What the review page says about the run itself: the PR header only when the run
   recorded it, the model that answered, a fallback note, and the failure text. */

test("a finished review shows the PR header and the model that answered", async ({ page }) => {
  await page.goto("/reviews/acme-web-142");
  await expect(page.getByText("fix/metrics-bind")).toBeVisible();
  await expect(page.getByText("by acme-dev")).toBeVisible();
  await expect(page.getByText(/^3 files/)).toBeVisible();
  await expect(page.getByTestId("model-used")).toContainText("model used · gpt-6.1-sol");
  await expect(page.getByTestId("fallback-note")).toHaveCount(0);
  await expect(page.getByTestId("run-error")).toHaveCount(0);
});

test("a fallback run names both models and the reason", async ({ page }) => {
  await page.goto("/reviews/acme-web-fallback");
  await expect(page.getByTestId("model-used")).toContainText("model used · gpt-6.1-sol");
  const note = page.getByTestId("fallback-note");
  await expect(note).toContainText("claude-sonnet-5-5");
  await expect(note).toContainText("gpt-6.1-sol");
  await expect(note).toContainText("529 overloaded");
  await page.screenshot({ path: "e2e/screenshots/run-fallback.png", fullPage: true });
});

test("a failed review shows its error and does not claim a model answered", async ({ page }) => {
  await page.goto("/reviews/acme-web-error");
  const failure = page.getByTestId("run-error");
  await expect(failure).toContainText("Review failed");
  await expect(failure).toContainText("BackendNotConfigured");
  await expect(page.getByTestId("model-used")).toContainText("no model ran");
  await expect(page.getByText("Not reviewed").first()).toBeVisible();
  await page.screenshot({ path: "e2e/screenshots/run-error.png", fullPage: true });
});

test("a header the run never recorded is hidden, not shown as a dash or zero", async ({ page }) => {
  await page.goto("/reviews/acme-web-bare");
  await expect(page.getByRole("heading", { name: /Bind metrics endpoint/ })).toBeVisible();
  await expect(page.getByText(/by acme-dev/)).toHaveCount(0);
  await expect(page.getByText(/^\d+ files?/)).toHaveCount(0);
  await expect(page.getByText("—", { exact: true })).toHaveCount(0);
});

test("the trigger screen shows why the API refused to start a review", async ({ page }) => {
  await page.route("**/api/argus/reviews", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "No model provider configured. Set OPENROUTER_API_KEY." }),
    });
  });
  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("acme/api#5");
  await page.getByRole("button", { name: "Run review" }).click();
  await expect(page.getByTestId("run-readout")).toContainText("No model provider configured");
  await expect(page).toHaveURL(/\/trigger$/);
});
