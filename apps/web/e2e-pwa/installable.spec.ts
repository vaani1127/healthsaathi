import { expect, test } from "@playwright/test";

test("the production build is installable", async ({ page, context }) => {
  await page.goto("/login");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });

  const manifest = await (await page.request.get("/manifest.webmanifest")).json();
  expect(manifest.display).toBe("standalone");
  expect(manifest.start_url).toBe("/");
  expect(manifest.icons.map((i: { sizes: string }) => i.sizes)).toEqual(
    expect.arrayContaining(["192x192", "512x512"]),
  );

  // The same check Chrome uses before offering "Install app".
  const cdp = await context.newCDPSession(page);
  const { installabilityErrors } = await cdp.send("Page.getInstallabilityErrors");
  expect(installabilityErrors).toEqual([]);
});

test("the app shell loads offline", async ({ page, context }) => {
  await page.goto("/login");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload();
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await context.setOffline(false);
});
