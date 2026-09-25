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

test("Lab owner sees peer secrets once and clears them on refresh, incomplete create, error, and reload", async ({ page }) => {
  const members: { subject: string; role: string }[] = [];
  const peers: { id: string; name?: string }[] = [{ id: "existing-peer" }];
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner", lab_id: "lab-a", scopes: ["lab:admin"] } }));
  await page.route("**/v1/labs/lab-a/members", async (route) => {
    if (route.request().method() === "POST") {
      const member = route.request().postDataJSON();
      members.push(member);
      return route.fulfill({ status: 201, json: member });
    }
    return route.fulfill({ json: { members } });
  });
  await page.route("**/v1/labs/lab-a/budget", (route) => route.fulfill({ json: { budget_thb: null } }));
  await page.route("**/v1/labs/lab-a/api-keys", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/labs/lab-a/usage?period=monthly", (route) => route.fulfill({ json: { period: "monthly", cost_thb: 0 } }));
  await page.route("**/v1/labs/lab-a/peers", async (route) => {
    if (route.request().method() !== "POST") return route.fulfill({ json: peers });
    const { name } = route.request().postDataJSON();
    if (name === "server-error") return route.fulfill({ status: 500, json: { detail: "creation failed" } });
    const created = { id: `peer-${peers.length}`, name };
    peers.push(created);
    if (name === "missing-secret") {
      return route.fulfill({ status: 201, json: { ...created, scopes: ["runs:read", "runs:write"] } });
    }
    return route.fulfill({
      status: 201,
      json: { ...created, secret: `secret-${name}`, scopes: ["runs:read", "runs:write"] },
    });
  });

  await page.goto("/labs/lab-a");
  const peerSection = page.locator("section").filter({ has: page.getByRole("heading", { name: "Peers" }) });
  await expect(peerSection.getByRole("list").getByText("existing-peer")).toBeVisible();
  await peerSection.getByLabel("ชื่อ peer").fill("worker-one");
  await peerSection.getByRole("button", { name: "เพิ่ม peer" }).click();
  await expect(peerSection.getByText("secret-worker-one", { exact: true })).toBeVisible();
  await expect(peerSection.getByRole("list").getByText("secret-worker-one", { exact: true })).toHaveCount(0);
  await expect(peerSection.getByRole("list").getByText("worker-one", { exact: true })).toBeVisible();

  await page.getByLabel("OIDC subject").fill("researcher-1");
  await page.getByRole("button", { name: "เพิ่มสมาชิก" }).click();
  await expect(peerSection.getByText("secret-worker-one", { exact: true })).toHaveCount(0);

  await peerSection.getByLabel("ชื่อ peer").fill("worker-two");
  await peerSection.getByRole("button", { name: "เพิ่ม peer" }).click();
  await expect(peerSection.getByText("secret-worker-two", { exact: true })).toBeVisible();
  await peerSection.getByLabel("ชื่อ peer").fill("missing-secret");
  await peerSection.getByRole("button", { name: "เพิ่ม peer" }).click();
  await expect(peerSection.getByText("secret-worker-two", { exact: true })).toHaveCount(0);
  await expect(page.locator("main p[role=alert]")).toContainText("API ไม่ส่ง secret");

  await peerSection.getByLabel("ชื่อ peer").fill("server-error");
  await peerSection.getByRole("button", { name: "เพิ่ม peer" }).click();
  await expect(peerSection.getByText(/secret-/)).toHaveCount(0);
  await expect(page.locator("main p[role=alert]")).toContainText("500");

  await peerSection.getByLabel("ชื่อ peer").fill("reload-check");
  await peerSection.getByRole("button", { name: "เพิ่ม peer" }).click();
  await expect(peerSection.getByText("secret-reload-check", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("secret-reload-check", { exact: true })).toHaveCount(0);
});
