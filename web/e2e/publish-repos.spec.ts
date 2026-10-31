import { expect, test } from "@playwright/test";

test("posting buttons move a repo and save the source", async ({ page }) => {
  await page.goto("/config", { waitUntil: "networkidle" });

  const reviewList = page.getByTestId("publish-review-list");
  const postList = page.getByTestId("publish-post-list");
  const rowId = "publish-row-acme/billing";

  await expect(reviewList.getByTestId(rowId)).toBeVisible();
  await reviewList.getByRole("button", { name: "Post acme/billing to GitHub", exact: true }).click();

  await expect(postList.getByTestId(rowId)).toBeVisible();
  await expect(reviewList.getByTestId(rowId)).toHaveCount(0);
  await expect(page.getByTestId("publish-source")).toHaveText("Source: dashboard");

  await postList.getByRole("button", { name: "Stop posting acme/billing", exact: true }).click();
  await expect(reviewList.getByTestId(rowId)).toBeVisible();
  await expect(postList.getByTestId(rowId)).toHaveCount(0);
  await expect(reviewList.getByRole("button", { name: "Post acme/billing to GitHub", exact: true })).toBeEnabled();
});

test("dragging a repo enables posting", async ({ page }) => {
  await page.goto("/config");
  await page.waitForLoadState("networkidle");

  const reviewList = page.getByTestId("publish-review-list");
  const postList = page.getByTestId("publish-post-list");
  const rowId = "publish-row-acme/web";

  await page.getByTestId(rowId).dragTo(postList);

  await expect(postList.getByTestId(rowId)).toBeVisible();
  await expect(reviewList.getByTestId(rowId)).toHaveCount(0);
  await expect(page.getByTestId("publish-source")).toHaveText("Source: dashboard");
});

test("missing bot access links to the repo access settings", async ({ page }) => {
  await page.goto("/config", { waitUntil: "networkidle" });

  const row = page.getByTestId("publish-post-list").getByTestId("publish-row-acme/auth");
  const warning = row.getByTestId("publish-warning-acme/auth");

  await expect(warning).toBeVisible();
  await expect(warning).toContainText("bot has no write access");
  await expect(warning.getByRole("link", { name: "Open access settings", exact: true }))
    .toHaveAttribute("href", "https://github.com/acme/auth/settings/access");
});

test("posting mode explains that dry run does not post", async ({ page }) => {
  await page.goto("/config", { waitUntil: "networkidle" });

  await expect(page.getByTestId("publish-mode")).toHaveText("Posting: dry run");
  await expect(page.getByTestId("publish-mode-note"))
    .toHaveText("Dry run: reviews are rendered and stored but not posted.");
});
