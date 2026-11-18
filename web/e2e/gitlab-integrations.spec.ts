import { expect, test } from "@playwright/test";

test("GitLab card reports the configured connection and policy", async ({ page }) => {
  await page.goto("/integrations");
  const card = page.locator(".panel").filter({ hasText: "token configured" }).filter({ hasText: "GitLab" });
  for (const value of ["gitlab.test", "https://gitlab.test/gitlab", "review-bot (bot)", "dry-run", "none"]) {
    await expect(card.getByText(value, { exact: true })).toBeVisible();
  }
  await expect(card.getByText("Publish projects", { exact: true }).locator("..")).toHaveText("Publish projects2");
  await expect(card.getByText("Allow entries", { exact: true }).locator("..")).toHaveText("Allow entries3");
  await expect(page.getByText("How Argus reaches GitHub, GitLab and your model provider, and how clients reach Argus.", { exact: true })).toBeVisible();
  await expect(page.getByText("GitLab", { exact: true })).toHaveCount(2);
});

for (const width of [375, 768, 1280]) {
  test(`GitLab integrations at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/integrations", { waitUntil: "networkidle" });
    await expect(page.getByText("https://gitlab.test/gitlab", { exact: true })).toBeVisible();
    await page.screenshot({ path: `e2e/screenshots/gitlab-integrations-${width}.png`, fullPage: true });
  });
}
