import { expect, test } from "@playwright/test";

test("GitLab off preserves the integration screen", async ({ page }) => {
  await page.goto("/integrations");
  await expect(page.getByText("GitLab", { exact: true })).toHaveCount(0);
  await expect(page.getByText("How Argus reaches GitHub and your model provider, and how clients reach Argus.", { exact: true })).toBeVisible();
});

test("GitLab off keeps the original trigger label and validation", async ({ page }) => {
  await page.route("**/api/argus/integrations", (route) => route.fulfill({ json: {} }));
  let posts = 0;
  await page.route("**/api/argus/reviews", (route) => { posts += 1; return route.fulfill({ status: 500, json: {} }); });
  await page.goto("/trigger");
  await expect(page.locator('label[for="pr-target"]')).toHaveText("Pull request");
  await expect(page.getByLabel("PR to review")).toHaveAttribute("placeholder", "owner/repo#pr");
  await expect(page.getByTestId("run-readout")).toHaveText("idle · enter a pull request above to start");
  for (const input of ["foo", "gitlab:gitlab.test#1#2"]) {
    await page.getByLabel("PR to review").fill(input);
    await page.getByTestId("run-button").click();
    await expect(page.getByTestId("input-error")).toHaveText("Use the form owner/repo#pr, e.g. acme/api#5");
  }
  expect(posts).toBe(0);
});
