import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function scan(page: import("@playwright/test").Page, name: string, findings: string[]) {
  const { violations } = await new AxeBuilder({ page }).analyze();
  for (const violation of violations) {
    for (const node of violation.nodes) {
      findings.push(`${name}: ${violation.id} (${violation.impact}) ${node.target.join(", ")} ${node.html} — ${node.failureSummary}`);
    }
  }
}

test.skip(process.env.SCILAB_DEMO_E2E !== "1", "Run only against the explicit pnpm demo command");

test("local demo walks from goal to approval, evidence, and Lab administration", async ({ page }) => {
  const findings: string[] = [];
  await page.request.post("http://127.0.0.1:18787/v1/demo/reset");
  await page.goto("/");
  await expect(page.locator("main")).toBeVisible();
  await scan(page, "landing", findings);
  await expect(page.getByText(/โหมดทดลองในเครื่อง/)).toBeVisible();
  await page.getByRole("link", { name: /เริ่มงานวิจัย/ }).first().click();
  await page.getByLabel("โจทย์วิจัย").fill("สรุปงานวิจัยตัวอย่าง");
  await scan(page, "new Run", findings);
  await page.getByRole("button", { name: "เริ่ม Run" }).click();
  await expect(page).toHaveURL(/\/runs\/demo-run-\d+$/);
  const runPath = new URL(page.url()).pathname;
  await expect(page.getByText("รอการอนุมัติ")).toBeVisible();
  await page.getByRole("button", { name: "อนุมัติ" }).click();
  await expect(page.getByText(/รายงานตัวอย่าง/).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "เปิด/ดาวน์โหลด" }).first()).toBeVisible();
  await scan(page, "completed Run", findings);
  await page.goto("/labs/demo-lab");
  await expect(page.getByText("ยังไม่ได้ตั้งงบ")).toBeVisible();
  await scan(page, "Lab admin", findings);
  await page.getByLabel("งบ (THB) · เว้นว่างเพื่อล้างค่า").fill("250");
  await page.getByRole("button", { name: "บันทึกงบ" }).click();
  await expect(page.getByText("เพดานปัจจุบัน 250 THB")).toBeVisible();
  await page.getByLabel("ชื่อ key").fill("local-demo-key");
  await page.getByRole("button", { name: "สร้าง key" }).click();
  await expect(page.getByText(/แสดงครั้งเดียว/)).toBeVisible();
  await expect(page.getByText("local-demo-key")).toBeVisible();
  await page.getByLabel("ชื่อ peer").fill("local-demo-peer");
  await page.getByRole("button", { name: "เพิ่ม peer" }).click();
  await expect(page.getByText("local-demo-peer")).toBeVisible();
  expect(findings, findings.join("\n\n")).toEqual([]);
  for (const width of [320, 375, 414, 768]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ["/", "/runs/new", runPath, "/labs/demo-lab"]) {
      await page.goto(path);
      await expect(page.locator("main")).toBeVisible();
      const dimensions = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }));
      expect(dimensions.document, `${path} at ${width}px`).toBeLessThanOrEqual(dimensions.viewport);
    }
  }
});
