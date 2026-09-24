import { expect, test } from "@playwright/test";

const approvalFrame = 'id: 3\nevent: approval.required\ndata: {"seq":3,"type":"approval.required","payload":{"approval_id":"approval-1","action":"report.publish","reason":"ตรวจการเผยแพร่","policy_rule":"review","expires_at":"2030-01-01T00:00:00Z","preview":{}}}\n\n';

test("uses public Run/event/artifact contracts for cost, approval and evidence", async ({ page }) => {
  let state = "awaiting_approval";
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner", lab_id: "lab-a", scopes: ["runs:read", "runs:approve", "artifacts:read"] } }));
  await page.route("**/v1/runs/run-1", (route) => route.fulfill({ json: { id: "run-1", lab_id: "lab-a", state } }));
  await page.route("**/v1/runs/run-1/artifacts", (route) => route.fulfill({ json: state === "completed" ? [
    { id: "report-1", kind: "report", metadata: { name: "report.md", content_type: "text/markdown" } },
    { id: "manifest-1", kind: "manifest", metadata: { name: "manifest.json", content_type: "application/json" } },
  ] : [] }));
  await page.route("**/v1/artifacts/report-1", (route) => route.fulfill({ json: { id: "report-1", kind: "report", metadata: { name: "report.md", content_type: "text/markdown" }, url: "https://objects.example/report" } }));
  await page.route("**/v1/artifacts/manifest-1", (route) => route.fulfill({ json: { id: "manifest-1", kind: "manifest", metadata: { name: "manifest.json", content_type: "application/json" }, url: "https://objects.example/manifest" } }));
  await page.route("**/v1/artifacts/report-1/content", (route) => route.fulfill({ body: "# รายงานตัวอย่าง", contentType: "text/markdown" }));
  await page.route("**/v1/artifacts/manifest-1/content", (route) => route.fulfill({ json: { goal: "ศึกษาหลักฐาน", claims: [{ id: "claim-1", text: "ข้อสรุป", evidence: ["https://example.org/source"], confidence: 0.8 }] } }));
  await page.route("**/v1/runs/run-1/events?from_seq=*", (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: 'id: 1\nevent: tool.finished\ndata: {"seq":1,"type":"tool.finished","payload":{"tool":"search"}}\n\nid: 2\nevent: cost.updated\ndata: {"seq":2,"type":"cost.updated","payload":{"tokens_in":10,"tokens_out":5,"llm_cost_thb":1.0,"compute_cost_thb":0.25,"budget_remaining_thb":null}}\n\n' + approvalFrame }));
  await page.route("**/v1/runs/run-1/approvals/approval-1", (route) => { state = "completed"; return route.fulfill({ json: { state } }); });
  await page.goto("/runs/run-1");
  await expect(page.getByText("search").first()).toBeVisible();
  await expect(page.getByText(/ค่าใช้จ่ายสะสม: 1.25 THB/)).toBeVisible();
  await page.getByRole("button", { name: "อนุมัติ" }).click();
  await expect(page.getByRole("heading", { name: "รายงานตัวอย่าง" })).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "เปิด/ดาวน์โหลด" }).first()).toHaveAttribute("href", "https://objects.example/report");
  await expect(page.getByRole("heading", { name: "Markdown" })).toBeVisible();
  await expect(page.getByText("ข้อสรุป", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "https://example.org/source" })).toBeVisible();
});

test("renders a Markdown pipe table as real cells without interpreting report HTML", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:viewer", lab_id: "lab-a", scopes: ["runs:read", "artifacts:read"] } }));
  await page.route("**/v1/runs/run-table", (route) => route.fulfill({ json: { id: "run-table", lab_id: "lab-a", state: "completed" } }));
  await page.route("**/v1/runs/run-table/artifacts", (route) => route.fulfill({ json: [{ id: "table-report", kind: "report", metadata: { name: "report.md", content_type: "text/markdown" } }] }));
  await page.route("**/v1/artifacts/table-report", (route) => route.fulfill({ json: { id: "table-report", kind: "report", metadata: { name: "report.md", content_type: "text/markdown" } } }));
  const report = "# ผลวิจัย\n\nสรุป **สำคัญ** และ $E=mc^2$\n\n| ตัวชี้วัด | ผล |\n| :--- | ---: |\n| ความแม่นยำ | <img src=x onerror=alert(1)> |\n\nสรุปท้ายรายงาน\n\n[อันตราย](javascript:alert(1))";
  await page.route("**/v1/artifacts/table-report/content", (route) => route.fulfill({ body: report, contentType: "text/markdown" }));
  await page.route("**/v1/runs/run-table/events?from_seq=*", (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.goto("/runs/run-table");
  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader")).toHaveText(["ตัวชี้วัด", "ผล"]);
  await expect(table.getByRole("cell")).toHaveText(["ความแม่นยำ", "<img src=x onerror=alert(1)>"]);
  await expect(table.locator("img")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "ผลวิจัย" })).toBeVisible();
  await expect(page.locator("strong").filter({ hasText: "สำคัญ" })).toBeVisible();
  await expect(page.locator(".katex")).toContainText("E");
  await expect(page.locator(".katex")).toContainText("mc");
  await expect(page.locator("img[src='x']")).toHaveCount(0);
  await expect(page.locator("a[href^='javascript:']")).toHaveCount(0);
  await expect(page.locator("pre").filter({ hasText: "# ผลวิจัย" })).toHaveCount(0);
});

test("viewer sees approval request but no decision buttons", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:viewer", lab_id: "lab-a", scopes: ["runs:read", "artifacts:read"] } }));
  await page.route("**/v1/runs/run-1", (route) => route.fulfill({ json: { id: "run-1", state: "awaiting_approval" } }));
  await page.route("**/v1/runs/run-1/artifacts", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/runs/run-1/events?from_seq=*", (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: approvalFrame }));
  await page.goto("/runs/run-1");
  await expect(page.getByText("บัญชีนี้ไม่มีสิทธิ์ตัดสินใจ")).toBeVisible();
  await expect(page.getByRole("button", { name: "อนุมัติ" })).toHaveCount(0);
});

test("rejects approval without showing completed evidence", async ({ page }) => {
  let state = "awaiting_approval";
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:owner", lab_id: "lab-a", scopes: ["runs:read", "runs:approve"] } }));
  await page.route("**/v1/runs/run-2", (route) => route.fulfill({ json: { id: "run-2", state } }));
  await page.route("**/v1/runs/run-2/artifacts", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/runs/run-2/events?from_seq=*", (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: approvalFrame }));
  await page.route("**/v1/runs/run-2/approvals/approval-1", (route) => { state = "cancelled"; return route.fulfill({ json: { state } }); });
  await page.goto("/runs/run-2");
  await page.getByRole("button", { name: "ปฏิเสธ" }).click();
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "รายงาน" })).toHaveCount(0);
});

test("resumes SSE from last sequence without duplicate steps", async ({ page }) => {
  const cursors: string[] = [];
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:viewer", lab_id: "lab-a", scopes: ["runs:read"] } }));
  await page.route("**/v1/runs/run-3", (route) => route.fulfill({ json: { id: "run-3", state: "running" } }));
  await page.route("**/v1/runs/run-3/artifacts", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/runs/run-3/events?from_seq=*", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("from_seq") || "0";
    cursors.push(cursor);
    return route.fulfill({ status: 200, contentType: "text/event-stream", body: cursor === "0" ? 'id: 1\nevent: tool.started\ndata: {"seq":1,"type":"tool.started","payload":{"tool":"search"}}\n\n' : "" });
  });
  await page.goto("/runs/run-3");
  await expect(page.getByText("search").first()).toBeVisible();
  await expect.poll(() => cursors.includes("1")).toBe(true);
  await expect(page.getByText("search").first()).toBeVisible();
});

test("shows LaTeX report and registered image when present", async ({ page }) => {
  await page.route("**/v1/me", (route) => route.fulfill({ json: { principal: "user:viewer", lab_id: "lab-a", scopes: ["runs:read", "artifacts:read"] } }));
  await page.route("**/v1/runs/run-4", (route) => route.fulfill({ json: { id: "run-4", state: "completed" } }));
  await page.route("**/v1/runs/run-4/artifacts", (route) => route.fulfill({ json: [
    { id: "tex-1", kind: "report", metadata: { name: "report.tex", content_type: "application/x-tex" } },
    { id: "image-1", kind: "image", metadata: { name: "figure.png", content_type: "image/png" } },
  ] }));
  await page.route("**/v1/artifacts/tex-1", (route) => route.fulfill({ json: { id: "tex-1", kind: "report", metadata: { name: "report.tex", content_type: "application/x-tex" }, url: "https://objects.example/report.tex" } }));
  await page.route("**/v1/artifacts/image-1", (route) => route.fulfill({ json: { id: "image-1", kind: "image", metadata: { name: "figure.png", content_type: "image/png" }, url: "https://objects.example/figure.png" } }));
  await page.route("**/v1/artifacts/tex-1/content", (route) => route.fulfill({ body: "\\section{Example}\n\\[E=mc^2\\]", contentType: "application/x-tex" }));
  await page.route("**/v1/runs/run-4/events?from_seq=*", (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.goto("/runs/run-4");
  await expect(page.getByRole("heading", { name: "LaTeX" })).toBeVisible();
  await expect(page.getByText("ตัวอย่าง source LaTeX (ยังไม่ได้ compile)")).toBeVisible();
  await expect(page.getByText(/\\section\{Example\}/)).toBeVisible();
  await expect(page.locator(".katex")).toContainText("mc");
  await expect(page.getByRole("img", { name: "figure.png" })).toHaveAttribute("src", "https://objects.example/figure.png");
});
