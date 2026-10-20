import { expect, test, type Page } from "@playwright/test";

/* Trigger-a-review flow. The backend is mocked deterministically with
   page.route so the running -> done -> route path and the error/validation
   paths are exercised without a live FastAPI + model call. */

const DONE_FINDINGS = [
  { file: "a.py", line: 3, severity: "critical", title: "one" },
  { file: "b.py", line: 9, severity: "major", title: "two" },
  { file: "c.py", line: 1, severity: "minor", title: "three" },
];

/** Mock POST /reviews -> 201 running, and GET /reviews/abc123 -> running on
 *  the first poll, done (3 findings) on every poll after. Returns a getter for
 *  how many POSTs fired so a test can assert no submit happened. */
function mockRunThenDone(page: Page) {
  let posts = 0;
  let getCount = 0;

  page.route("**/api/argus/reviews", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts += 1;
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ id: "abc123", status: "running" }),
    });
  });

  // The trigger polls the verdict document (the real API shape): a running
  // review returns {status:"running"}; a finished one carries engine.status and
  // counts.confirmed. ReviewDetail (the old target) has none of these.
  page.route("**/api/argus/reviews/abc123/verdict", async (route) => {
    getCount += 1;
    const body =
      getCount === 1
        ? { review_id: "abc123", status: "running" }
        : {
            review_id: "abc123",
            engine: { status: "done" },
            counts: { confirmed: 3 },
            findings: DONE_FINDINGS,
          };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });

  return { posts: () => posts };
}

test("running readout then a finding count, then routes to the detail", async ({ page }) => {
  mockRunThenDone(page);
  await page.goto("/trigger");

  await page.getByLabel("PR to review").fill("acme/api#5");
  await page.getByRole("button", { name: "Run review" }).click();

  const readout = page.getByTestId("run-readout");
  await expect(readout).toContainText(/running/i);
  await page.screenshot({ path: "e2e/screenshots/trigger-running.png", fullPage: true });

  await expect(readout).toContainText(/3 finding/i, { timeout: 8000 });
  await page.screenshot({ path: "e2e/screenshots/trigger-done.png", fullPage: true });

  await expect(page).toHaveURL(/\/reviews\/abc123$/, { timeout: 8000 });
});

test("button is disabled while a run is in flight", async ({ page }) => {
  mockRunThenDone(page);
  await page.goto("/trigger");

  await page.getByLabel("PR to review").fill("acme/api#5");
  const button = page.getByTestId("run-button");
  await button.click();
  // While the run is in flight the button is disabled and reads "Running".
  await expect(button).toBeDisabled();
  await expect(button).toContainText(/running/i);
});

test("an API error renders the error string in the readout", async ({ page }) => {
  page.route("**/api/argus/reviews", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ id: "abc123", status: "running" }),
    });
  });
  page.route("**/api/argus/reviews/abc123/verdict", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        review_id: "abc123",
        engine: { status: "error", error: "boom: bad repo" },
      }),
    });
  });

  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("acme/api#5");
  await page.getByRole("button", { name: "Run review" }).click();

  const readout = page.getByTestId("run-readout");
  await expect(readout).toContainText("boom: bad repo", { timeout: 8000 });
  await page.screenshot({ path: "e2e/screenshots/trigger-error.png", fullPage: true });
  // The run failed, so it must not navigate away.
  await expect(page).toHaveURL(/\/trigger$/);
});

test("malformed input is rejected client-side and fires no POST", async ({ page }) => {
  let posted = false;
  page.route("**/api/argus/reviews", async (route) => {
    if (route.request().method() === "POST") posted = true;
    return route.fallback();
  });

  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("acme/api"); // no #pr
  await page.getByRole("button", { name: "Run review" }).click();

  await expect(page.getByTestId("input-error")).toBeVisible();
  await page.screenshot({ path: "e2e/screenshots/trigger-malformed.png", fullPage: true });
  // Give any stray request a beat to land, then assert none did.
  await page.waitForTimeout(300);
  expect(posted).toBe(false);
  await expect(page).toHaveURL(/\/trigger$/);
});
