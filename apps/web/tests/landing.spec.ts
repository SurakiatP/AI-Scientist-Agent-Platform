import { expect, test } from "@playwright/test";

test("landing explains the TOR workflow and routes to a new Run", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/AI Scientist/);
  await expect(page.getByRole("heading", { level: 1, name: /จากโจทย์วิจัย.*สู่ผลลัพธ์ที่ตรวจสอบได้/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /ดูวิธีทำงาน/ })).toHaveAttribute("href", "#how-it-works");
  await expect(page.getByRole("link", { name: /เริ่มงานวิจัย/ }).first()).toHaveAttribute("href", "/runs/new");
  const how = page.locator("#how-it-works");
  await expect(how.getByRole("heading", { name: "ส่งโจทย์และข้อมูลตั้งต้น" })).toBeVisible();
  await expect(how.getByRole("heading", { name: "ติดตามขั้นตอนและการอนุมัติ" })).toBeVisible();
  await expect(how.getByRole("heading", { name: "ตรวจรายงานและหลักฐาน" })).toBeVisible();
});

test("landing stays usable at 320/375/414/768 px with keyboard and reduced motion", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const width of [320, 375, 414, 768]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    const dimensions = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }));
    expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
    await expect(page.getByRole("link", { name: /เริ่มงานวิจัย/ }).first()).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(page.locator(":focus-visible")).toHaveCount(1);
  }
});
