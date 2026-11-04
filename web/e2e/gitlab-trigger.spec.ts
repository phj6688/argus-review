import { expect, test, type Page } from "@playwright/test";

const gitlab = {
  host: "gitlab.test", baseUrl: "https://gitlab.test/gitlab", token: "configured",
  identity: { status: "ok", username: "review-bot", bot: true },
  publish: { mode: "dry-run", projectCount: 2 }, allowCount: 3, refusal: null,
};

async function capturePosts(page: Page) {
  const bodies: unknown[] = [];
  await page.route("**/api/argus/reviews", async (route) => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({ status: 201, json: { id: "test-run", status: "running" } });
  });
  await page.route("**/api/argus/reviews/test-run/verdict", (route) => route.fulfill({ json: { status: "running" } }));
  return bodies;
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/argus/integrations", (route) => route.fulfill({ json: { gitlab } }));
});

for (const [value, repo, pr] of [
  ["gitlab:GITLAB.TEST#42#7", "gitlab:gitlab.test#42", 7],
  ["gitlab:gitlab.test/group/sub/app#7", "gitlab:gitlab.test/group/sub/app", 7],
  ["https://gitlab.test/gitlab/group/sub/app/-/merge_requests/7/#note_1", "gitlab:gitlab.test/group/sub/app", 7],
  ["https://gitlab.test/gitlab/group/sub/app/-/merge_requests/007", "gitlab:gitlab.test/group/sub/app", 7],
  ["acme/api#5", "acme/api", 5],
] as const) {
  test(`accepts ${value} and sends the expected review body`, async ({ page }) => {
    const bodies = await capturePosts(page);
    await page.goto("/trigger");
    await expect(page.locator('label[for="pr-target"]')).toHaveText("Pull request or merge request");
    await expect(page.getByLabel("PR to review")).toHaveAttribute("placeholder", "owner/repo#pr");
    await page.getByLabel("PR to review").fill(value);
    if (repo.startsWith("gitlab:")) await expect(page.getByTestId("run-readout")).toContainText("merge request");
    await page.getByTestId("run-button").click();
    await expect.poll(() => bodies).toEqual([{ repo, pr }]);
    await expect(page.getByTestId("run-readout")).toContainText(`running review · ${repo}#${pr}`);
  });
}

for (const value of [
  "gitlab:other.test#42#7", "gitlab:gitlab.test#42#0", "gitlab:gitlab.test#042#7",
  "gitlab:gitlab.test#42#07", "gitlab:gitlab.test/project#7", "foo",
  "https://gitlab.test/gitlab2/group/app/-/merge_requests/7",
  "https://gitlab.test/gitlab/group/%61pp/-/merge_requests/7",
  "https://gitlab.test/gitlab/group/app/-/merge_requests/0",
  "https://gitlab.test/gitlab/app/-/merge_requests/7",
  "http://gitlab.test/gitlab/group/app/-/merge_requests/7",
  "https://user:password@gitlab.test/gitlab/group/app/-/merge_requests/7",
  "https://@gitlab.test/gitlab/group/app/-/merge_requests/7",
  "https://gitlab.test/gitlab/group/../app/-/merge_requests/7",
  "https://gitlab.test/gitlab/group//app/-/merge_requests/7",
  "https://gitlab.test/gitlab/group/app/-/merge_requests/١",
]) {
  test(`refuses ${value} without a review request`, async ({ page }) => {
    const bodies = await capturePosts(page);
    await page.goto("/trigger");
    await expect(page.locator('label[for="pr-target"]')).toHaveText("Pull request or merge request");
    await page.getByLabel("PR to review").fill(value);
    await page.getByTestId("run-button").click();
    await expect(page.getByTestId("input-error")).toContainText("owner/repo#pr (GitHub)");
    await expect(page.getByTestId("input-error")).toContainText("gitlab:gitlab.test#PROJECT_ID#iid");
    expect(bodies).toEqual([]);
  });
}

test("GitLab submission waits for settings and fetches them once", async ({ page }) => {
  let release!: () => void;
  let requests = 0;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/argus/integrations", async (route) => {
    requests += 1;
    await pending;
    await route.fulfill({ json: { gitlab } });
  });
  const bodies = await capturePosts(page);
  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("gitlab:gitlab.test#42#7");
  await page.getByTestId("run-button").click();
  await expect(page.getByLabel("PR to review")).toBeDisabled();
  await expect(page.getByTestId("run-button")).toBeDisabled();
  await expect(page.getByTestId("run-button")).toHaveText("Checking");
  expect(bodies).toEqual([]);
  await expect(page.getByTestId("input-error")).toHaveCount(0);
  release();
  await expect.poll(() => bodies).toEqual([{ repo: "gitlab:gitlab.test#42", pr: 7 }]);
  await expect(page.getByTestId("run-button")).toHaveText("Running");
  expect(requests).toBe(1);
});

test("GitLab submission retries settings after a failure", async ({ page }) => {
  let failFirst!: () => void;
  let requests = 0;
  const pending = new Promise<void>((resolve) => { failFirst = resolve; });
  await page.route("**/api/argus/integrations", async (route) => {
    requests += 1;
    if (requests === 1) {
      await pending;
      await route.fulfill({ status: 503, json: {} });
    } else {
      await route.fulfill({ json: { gitlab } });
    }
  });
  const bodies = await capturePosts(page);
  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("gitlab:gitlab.test#42#7");
  await page.getByTestId("run-button").click();
  await expect(page.getByTestId("run-button")).toHaveText("Checking");
  failFirst();
  await expect(page.getByTestId("input-error")).toHaveText("Could not read the GitLab settings. Try again.");
  await expect(page.getByLabel("PR to review")).toBeEnabled();
  await expect(page.getByTestId("run-button")).toBeEnabled();
  await expect(page.getByTestId("run-button")).toHaveText("Run review");
  expect(bodies).toEqual([]);
  await page.getByTestId("run-button").click();
  await expect.poll(() => bodies).toEqual([{ repo: "gitlab:gitlab.test#42", pr: 7 }]);
  expect(requests).toBe(2);
});

test("settings failure gives a retry error and sends no GitLab request", async ({ page }) => {
  await page.route("**/api/argus/integrations", (route) => route.fulfill({ status: 503, json: {} }));
  const bodies = await capturePosts(page);
  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("gitlab:gitlab.test#42#7");
  await page.getByTestId("run-button").click();
  await expect(page.getByTestId("input-error")).toHaveText("Could not read the GitLab settings. Try again.");
  expect(bodies).toEqual([]);
});

test("GitHub submission does not wait for settings", async ({ page }) => {
  await page.route("**/api/argus/integrations", (route) => route.abort());
  const bodies = await capturePosts(page);
  await page.goto("/trigger");
  await page.getByLabel("PR to review").fill("acme/api#5");
  await page.getByTestId("run-button").click();
  await expect.poll(() => bodies).toEqual([{ repo: "acme/api", pr: 5 }]);
});
