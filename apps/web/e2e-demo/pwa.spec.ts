import { expect, test } from "@playwright/test";

import { signIn } from "../e2e/support";

// SPEC 4 and 9 on the production build: installable, offline shell, today's queue offline, and a
// vitals draft that is kept while offline and sent once the connection is back.

test("the app is installable", async ({ page, context }) => {
  await page.goto("/login");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  const manifest = await (await page.request.get("/manifest.webmanifest")).json();
  expect(manifest.display).toBe("standalone");
  const cdp = await context.newCDPSession(page);
  const { installabilityErrors } = await cdp.send("Page.getInstallabilityErrors");
  expect(installabilityErrors).toEqual([]);
});

test("the shell loads offline", async ({ page, context }) => {
  await page.goto("/login");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload();
  await context.setOffline(true);
  await page.reload();
  await expect(page.locator("#email")).toBeVisible();
  await context.setOffline(false);
});

test("a vitals draft survives being offline and is sent later", async ({ page, context }) => {
  await signIn(page, "nurse");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  const queueLink = page.locator('a[href^="/nurse/patients/"]').first();
  await expect(queueLink).toBeVisible();
  await queueLink.click();
  await expect(page.getByLabel("BP systolic")).toBeVisible();

  await context.setOffline(true);
  await page.getByLabel("BP systolic").fill("118");
  await page.getByLabel("BP diastolic").fill("76");
  await page.getByLabel("Pulse").fill("72");
  await page.getByRole("button", { name: "Save vitals" }).click();
  await expect(page.getByText("No connection. Saved on this device")).toBeVisible();
  await expect(page.getByText("1 unsent vitals on this device.")).toBeVisible();

  await context.setOffline(false);
  await page.getByRole("button", { name: "Send now" }).click();
  await expect(page.getByText("1 unsent vitals on this device.")).toHaveCount(0);
  await expect(page.getByTestId("vitals-list")).toContainText("BP 118/76");
});

test("today's queue is readable offline after one online visit", async ({ page, context }) => {
  await signIn(page, "nurse");
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload();
  const first = page.locator('a[href^="/nurse/patients/"]').first();
  await expect(first).toBeVisible();
  // The link shows the token number, then the patient's name.
  const name = (await first.innerText()).split("\n").map((x) => x.trim()).filter(Boolean)[1] ?? "";
  await first.click();
  await context.setOffline(true);
  // Back to the queue inside the app: the queue request is answered from the offline cache.
  await page.goBack();
  await expect(page.getByText(name).first()).toBeVisible();
  await context.setOffline(false);
});
