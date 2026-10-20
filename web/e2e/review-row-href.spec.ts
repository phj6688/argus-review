import { expect, test } from "@playwright/test";

/* A row must link to its OWN review id, not the demo showcase id.
   Pick a row that is NOT the showcase PR — on the old code every row pointed at
   /reviews/acme-web-142, which 422-ed the int-typed backend route in
   prod and rendered a 500. The existing smoke test can't catch this because the
   showcase row legitimately resolves to that id; a non-showcase row is the
   discriminator. */

test("a non-showcase review row links to its own id", async ({ page }) => {
  await page.goto("/reviews", { waitUntil: "networkidle" });

  // The first fixture row: acme/infra#218, its own id is acme-infra-218.
  const rowLink = page
    .getByRole("link")
    .filter({ hasText: "Add auth rate-limit cache" })
    .first();

  await expect(rowLink).toHaveAttribute("href", "/reviews/acme-infra-218");
});
