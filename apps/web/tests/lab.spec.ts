import { expect, test } from "@playwright/test";

test("Lab owner can manage members and budget; last owner stays protected", async ({ page }) => {
  const members = [{ subject: "owner-1", role: "owner" }, { subject: "member-1", role: "viewer" }];
  let budget: string | null = null;
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner-1", lab_id: "lab-a", scopes: ["lab:admin"] } }));
  await page.route("**/v1/labs/lab-a/members", async (route) => {
    if (route.request().method() === "POST") {
      const member = route.request().postDataJSON();
      members.push(member);
      return route.fulfill({ status: 201, json: member });
    }
    return route.fulfill({ json: { members } });
  });
  await page.route("**/v1/labs/lab-a/members/*", async (route) => {
    if (route.request().url().endsWith("owner-1")) return route.fulfill({ status: 409, json: { detail: "final owner protected" } });
    if (route.request().method() === "PATCH") {
      const member = members.find((item) => route.request().url().endsWith(item.subject));
      if (!member) return route.fulfill({ status: 404, json: { detail: "member not found" } });
      member.role = route.request().postDataJSON().role;
      return route.fulfill({ json: member });
    }
    return route.fulfill({ status: 204, body: "" });
  });
  await page.route("**/v1/labs/lab-a/budget", async (route) => {
    if (route.request().method() === "PUT") {
      budget = route.request().postDataJSON().budget_thb;
      expect(budget).toBe("123456789012345.6789");
    }
    return route.fulfill({ json: { budget_thb: budget } });
  });
  let keys = [{ id: "existing-key", name: "existing-key" }];
  await page.route("**/v1/labs/lab-a/api-keys*", (route) => {
    if (route.request().method() === "DELETE") {
      expect(new URL(route.request().url()).searchParams.get("key_id")).toBe("existing-key");
      keys = [];
      return route.fulfill({ status: 204, body: "" });
    }
    return route.fulfill({ json: route.request().method() === "POST" ? { id: "key-1" } : keys });
  });
  await page.route("**/v1/labs/lab-a/peers", (route) => route.fulfill({ json: [{ id: "existing-peer", name: "Existing peer" }] }));
  await page.route("**/v1/labs/lab-a/usage?period=monthly", (route) => route.fulfill({ json: { period: "monthly", cost_thb: 0 } }));
  await page.goto("/labs/lab-a");
  await expect(page.getByText("Existing peer")).toBeVisible();
  await expect(page.getByText("existing-key")).toBeVisible();
  await page.getByRole("listitem").filter({ hasText: "existing-key" }).getByRole("button", { name: "ลบ" }).click();
  await expect(page.getByText("existing-key")).toHaveCount(0);
  await expect(page.getByRole("listitem").filter({ hasText: "owner-1" }).getByText("owner-1")).toBeVisible();
  await expect(page.getByText("member-1")).toBeVisible();
  await page.getByLabel("บทบาทของ member-1").selectOption("researcher");
  await expect(page.getByRole("listitem").filter({ hasText: "member-1" }).locator("small")).toHaveText("researcher");
  await expect(page.getByText("ยังไม่ได้ตั้งงบ")).toBeVisible();
  await page.getByLabel("OIDC subject").fill("subject-2");
  await page.getByRole("button", { name: "เพิ่มสมาชิก" }).click();
  await expect(page.getByText("subject-2")).toBeVisible();
  await page.getByLabel("งบ (THB) · เว้นว่างเพื่อล้างค่า").fill("123456789012345.6789");
  await page.getByRole("button", { name: "บันทึกงบ" }).click();
  await expect(page.getByText("เพดานปัจจุบัน 123456789012345.6789 THB")).toBeVisible();
  await page.getByLabel("ชื่อ key").fill("ci");
  await page.getByRole("button", { name: "สร้าง key" }).click();
  await expect(page.getByText(/API ไม่ส่ง secret/)).toBeVisible();
  await expect(page.getByText("แสดงครั้งเดียว:")).toHaveCount(0);
  await page.getByRole("button", { name: "นำออก" }).first().click();
  await expect(page.locator("main [role=alert]")).toContainText("409");
});

test("viewer never sees Lab mutation controls", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:viewer", lab_id: "lab-a", scopes: ["runs:read"] } }));
  await page.goto("/labs/lab-a");
  await expect(page.getByText("ไม่สามารถเปิดส่วนจัดการ Lab")).toBeVisible();
  await expect(page.getByRole("button", { name: "เพิ่มสมาชิก" })).toHaveCount(0);
});
