import { expect, test } from "@playwright/test";

for (const [id, path, iid] of [["gitlab-app", "group/sub/app", 7], ["gitlab-api", "team/api", 8]] as const) {
  test(`${id} links to the configured base prefix and live project path`, async ({ page }) => {
    await page.goto(`/reviews/${id}`);
    await expect(page.getByText(path, { exact: true })).toBeVisible();
    await expect(page.getByText("MR", { exact: true })).toHaveAttribute("data-tip", "GitLab merge request");
    const link = page.getByRole("link", { name: "Open merge request" });
    await expect(link).toHaveAttribute("href", `https://gitlab.test/gitlab/${path}/-/merge_requests/${iid}`);
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    await expect(page.locator('[data-tip^="Argus overall decision"]')).toHaveAttribute("data-tip", /merge request/);
  });
}

test("a key-only title has no merge request link", async ({ page }) => {
  await page.goto("/reviews/gitlab-key");
  await expect(page.getByText("gitlab:gitlab.test#44", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open merge request" })).toHaveCount(0);
  await expect(page.getByText("Link unavailable", { exact: true })).toBeVisible();
});

test("a review from another GitLab host has no merge request link", async ({ page }) => {
  await page.goto("/reviews/gitlab-other");
  await expect(page.getByText("team/other", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open merge request" })).toHaveCount(0);
  await expect(page.getByText("Link unavailable", { exact: true })).toBeVisible();
});

test("a GitHub detail has no MR chip or merge request link", async ({ page }) => {
  await page.goto("/reviews/github-web");
  await expect(page.getByRole("heading", { name: "Bind metrics endpoint to loopback only" })).toBeVisible();
  await expect(page.getByText("MR", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open merge request" })).toHaveCount(0);
  await expect(page.getByText("Link unavailable", { exact: true })).toHaveCount(0);
});

for (const width of [375, 768, 1280]) {
  test(`merge request detail at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/reviews/gitlab-app", { waitUntil: "networkidle" });
    await expect(page.getByRole("link", { name: "Open merge request" })).toBeVisible();
    await page.screenshot({ path: `e2e/screenshots/gitlab-detail-${width}.png`, fullPage: true });
  });
}
