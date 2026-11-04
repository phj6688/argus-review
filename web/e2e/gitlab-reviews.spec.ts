import { expect, test } from "@playwright/test";

test("GitLab rows show paths and MR chips while GitHub rows stay unchanged", async ({ page }) => {
  await page.goto("/reviews");
  for (const [id, label] of [["gitlab-app", "group/sub/app"], ["gitlab-api", "team/api"], ["gitlab-key", "gitlab:gitlab.test#44"]]) {
    const row = page.locator(`a[href="/reviews/${id}"]`);
    await expect(row.locator("div").first()).toContainText(label);
    await expect(row.getByText("MR", { exact: true })).toHaveAttribute("data-tip", "GitLab merge request");
    await expect(row.locator("a")).toHaveCount(0);
    await expect(row.locator('[data-tip*="GitLab"]').last()).not.toHaveAttribute("data-tip", /GitHub/);
  }
  for (const id of ["github-infra", "github-web"]) {
    const row = page.locator(`a[href="/reviews/${id}"]`);
    await expect(row.getByText("MR", { exact: true })).toHaveCount(0);
    await expect(row.locator('[data-tip*="GitHub"]')).toHaveCount(1);
  }
  await expect(page.locator('a[href*="merge_requests"]')).toHaveCount(0);
});

test("search matches a GitLab project path and its canonical key", async ({ page }) => {
  await page.goto("/reviews");
  await page.getByLabel("Search reviews").fill("group/sub/app");
  await expect(page.locator('a[href="/reviews/gitlab-app"]')).toBeVisible();
  await expect(page.locator('a[href="/reviews/github-infra"]')).toHaveCount(0);
  await page.getByLabel("Search reviews").fill("gitlab:gitlab.test#43");
  await expect(page.locator('a[href="/reviews/gitlab-api"]')).toBeVisible();
});

for (const width of [375, 768, 1280]) {
  test(`review list at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/reviews", { waitUntil: "networkidle" });
    await expect(page.locator('a[href="/reviews/gitlab-app"]').getByText("MR", { exact: true })).toBeVisible();
    await page.screenshot({ path: `e2e/screenshots/gitlab-reviews-${width}.png`, fullPage: true });
  });
}
