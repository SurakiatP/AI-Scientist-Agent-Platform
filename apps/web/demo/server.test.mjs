import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createDemoServer } from "./server.mjs";

const server = createDemoServer();
let base;
before(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

test("demo flow is contract-shaped and final owner is protected", async () => {
  const me = await (await fetch(`${base}/v1/me`)).json();
  assert.equal(me.lab_id, "demo-lab");
  assert.equal(me.demo, true);

  const submitted = await fetch(`${base}/v1/labs/demo-lab/runs`, {
    method: "POST",
    headers: { "content-type": "application/json", "idempotency-key": "demo-test" },
    body: JSON.stringify({ goal: "ตัวอย่าง", inputs: [], skill_packs: ["general-research"], budget: { thb: 10, max_minutes: 5 } }),
  });
  assert.equal(submitted.status, 202);
  const { id } = await submitted.json();
  assert.ok(id);
  const repeated = await fetch(`${base}/v1/labs/demo-lab/runs`, {
    method: "POST", headers: { "content-type": "application/json", "idempotency-key": "demo-test" },
    body: JSON.stringify({ goal: "ตัวอย่าง", inputs: [], skill_packs: ["general-research"], budget: { thb: 10, max_minutes: 5 } }),
  });
  assert.equal((await repeated.json()).id, id);
  await new Promise((resolve) => setTimeout(resolve, 350));
  const run = await (await fetch(`${base}/v1/runs/${id}`)).json();
  assert.equal(run.state, "awaiting_approval");

  const decision = await fetch(`${base}/v1/runs/${id}/approvals/demo-approval`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ decision: "approve" }),
  });
  assert.equal(decision.status, 200);
  const finished = await (await fetch(`${base}/v1/runs/${id}`)).json();
  assert.equal(finished.state, "completed");
  assert.equal(finished.report_markdown, undefined);
  assert.match(await (await fetch(`${base}/v1/artifacts/demo-report/content`)).text(), /ตัวอย่าง/);
  const events = await fetch(`${base}/v1/runs/${id}/events?from_seq=3`);
  const reader = events.body.getReader();
  const chunk = new TextDecoder().decode((await reader.read()).value);
  assert.match(chunk, /run.completed/);
  await reader.cancel();

  const owner = await fetch(`${base}/v1/labs/demo-lab/members/demo-owner`, { method: "DELETE" });
  assert.equal(owner.status, 409);
  const budget = await (await fetch(`${base}/v1/labs/demo-lab/budget`)).json();
  assert.equal(budget.budget_thb, null);
});

test("rejecting a requested approval never publishes report artifacts", async () => {
  const submission = await fetch(`${base}/v1/labs/demo-lab/runs`, {
    method: "POST", headers: { "content-type": "application/json", "idempotency-key": "reject-test" },
    body: JSON.stringify({ goal: "ปฏิเสธ", inputs: [], skill_packs: ["general-research"], budget: { thb: 5, max_minutes: 5 } }),
  });
  const { id } = await submission.json();
  await new Promise((resolve) => setTimeout(resolve, 350));
  const response = await fetch(`${base}/v1/runs/${id}/approvals/demo-approval`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ decision: "reject" }),
  });
  assert.equal(response.status, 200);
  const run = await (await fetch(`${base}/v1/runs/${id}`)).json();
  assert.equal(run.state, "cancelled");
  const artifacts = await (await fetch(`${base}/v1/runs/${id}/artifacts`)).json();
  assert.deepEqual(artifacts, []);
});
