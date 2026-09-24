import { expect, test } from "@playwright/test";

test("submits a research goal with approved pack, file reference and budget once", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner", lab_id: "demo-lab", scopes: ["runs:write"] } }));
  await page.route("**/v1/labs/demo-lab/skills", (route) => route.fulfill({ json: [{ name: "general-research" }, { name: "blocked", approved: false }] }));
  await page.route("**/v1/labs/demo-lab/inputs", (route) => route.fulfill({ json: { artifact_id: "input-1" } }));
  let submissions = 0;
  await page.route("**/v1/labs/demo-lab/runs", async (route) => {
    submissions++;
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    expect(route.request().postDataJSON()).toMatchObject({ goal: "ศึกษาหลักฐาน", inputs: ["input-1"], skill_packs: ["general-research"], budget: { thb: 50, max_minutes: 30 } });
    await route.fulfill({ status: 202, json: { id: "run-1", state: "queued" } });
  });
  await page.route("**/v1/runs/run-1", (route) => route.fulfill({ json: { id: "run-1", state: "queued" } }));
  await page.route("**/v1/runs/run-1/**", (route) => route.fulfill({ json: [] }));
  await page.goto("/runs/new");
  await expect(page.getByRole("option", { name: "blocked" })).toHaveCount(0);
  await page.getByLabel("โจทย์วิจัย").fill("ศึกษาหลักฐาน");
  await page.locator("#files").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("notes") });
  await page.getByLabel("งบสูงสุด (THB)").fill("50");
  await page.getByLabel("เวลาสูงสุด (นาที)").fill("30");
  await page.getByRole("button", { name: "เริ่ม Run" }).click();
  await expect(page).toHaveURL(/\/runs\/run-1$/);
  expect(submissions).toBe(1);
});

test("shows authentication failure without manufacturing a session", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ status: 401, json: { detail: "unauthorized" } }));
  await page.goto("/runs/new");
  await expect(page.locator("main [role=alert]")).toContainText("เข้าสู่ระบบ");
  await expect(page.getByRole("button", { name: "เริ่ม Run" })).toHaveCount(0);
});

test("failed input upload does not create a Run and can be retried", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner", lab_id: "demo-lab", scopes: ["runs:write"] } }));
  await page.route("**/v1/labs/demo-lab/skills", (route) => route.fulfill({ json: [{ name: "general-research" }] }));
  let uploads = 0;
  await page.route("**/v1/labs/demo-lab/inputs", (route) => route.fulfill(
    ++uploads === 1 ? { status: 503, json: { detail: "unavailable" } } : { json: { artifact_id: "input-1" } },
  ));
  let submissions = 0;
  await page.route("**/v1/labs/demo-lab/runs", (route) => {
    submissions++;
    expect(route.request().postDataJSON().inputs).toEqual(["input-1"]);
    return route.fulfill({ status: 202, json: { id: "run-1", state: "queued" } });
  });
  await page.route("**/v1/runs/run-1", (route) => route.fulfill({ json: { id: "run-1", state: "queued" } }));
  await page.route("**/v1/runs/run-1/**", (route) => route.fulfill({ json: [] }));
  await page.goto("/runs/new");
  await page.getByLabel("โจทย์วิจัย").fill("ศึกษาหลักฐาน");
  await page.locator("#files").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("notes") });
  await page.getByRole("button", { name: "เริ่ม Run" }).click();
  await expect(page.locator("main [role=alert]")).toContainText("503");
  expect(submissions).toBe(0);
  await page.getByRole("button", { name: "เริ่ม Run" }).click();
  await expect(page).toHaveURL(/\/runs\/run-1$/);
  expect(uploads).toBe(2);
  expect(submissions).toBe(1);
});

test("retrying an uncertain Run response reuses the idempotency key", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner", lab_id: "demo-lab", scopes: ["runs:write"] } }));
  await page.route("**/v1/labs/demo-lab/skills", (route) => route.fulfill({ json: [{ name: "general-research" }] }));
  const keys: string[] = [];
  await page.route("**/v1/labs/demo-lab/runs", (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    return route.fulfill(keys.length === 1
      ? { status: 503, json: { detail: "unavailable" } }
      : { status: 202, json: { id: "run-1", state: "queued" } });
  });
  await page.route("**/v1/runs/run-1", (route) => route.fulfill({ json: { id: "run-1", state: "queued" } }));
  await page.route("**/v1/runs/run-1/**", (route) => route.fulfill({ json: [] }));
  await page.goto("/runs/new");
  await page.getByLabel("โจทย์วิจัย").fill("ศึกษาหลักฐาน");
  await page.getByRole("button", { name: "เริ่ม Run" }).click();
  await expect(page.locator("main [role=alert]")).toContainText("503");
  await page.getByRole("button", { name: "เริ่ม Run" }).click();
  await expect(page).toHaveURL(/\/runs\/run-1$/);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
});
