import { createServer } from "node:http";

const send = (res, status, value) => {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*" });
  res.end(JSON.stringify(value));
};

const read = async (req) => {
  let body = "";
  for await (const chunk of req) body += chunk;
  return body ? JSON.parse(body) : {};
};

export function createDemoServer() {
  const members = [{ subject: "demo-owner", role: "owner" }];
  const keys = [];
  const peers = [];
  const runs = new Map();
  const idempotency = new Map();
  let budget = null;
  let nextKey = 1;
  const streams = new Map();

  function event(run, type, payload = {}) {
    const record = { event_id: `demo-${run.events.length + 1}`, run_id: run.id, lab_id: "demo-lab", seq: run.events.length + 1, type, payload, ts: new Date().toISOString() };
    run.events.push(record);
    for (const stream of streams.get(run.id) || []) stream.write(`id: ${record.seq}\nevent: ${type}\ndata: ${JSON.stringify(record)}\n\n`);
  }

  return createServer(async (req, res) => {
    const url = new URL(req.url || "/", "http://localhost");
    const path = url.pathname;
    const method = req.method;
    try {
      if (path === "/v1/demo/reset" && method === "POST") {
        members.splice(1);
        keys.length = 0;
        peers.length = 0;
        runs.clear();
        idempotency.clear();
        budget = null;
        nextKey = 1;
        return send(res, 200, { demo: true, reset: true });
      }
      if (path === "/v1/me" && method === "GET") return send(res, 200, { principal: "user:demo-owner", lab_id: "demo-lab", scopes: ["runs:read", "runs:write", "runs:approve", "artifacts:read", "lab:admin"], demo: true });
      if (path === "/v1/labs/demo-lab/skills" && method === "GET") return send(res, 200, [{ id: "general-research", name: "General Research", approved: true }]);
      if (path === "/v1/labs/demo-lab/inputs" && method === "POST") {
        const body = await read(req);
        const id = `demo-input-${Date.now()}`;
        return send(res, 200, { artifact_id: id, name: body.name || "ตัวอย่างไฟล์", demo: true });
      }
      if (path === "/v1/labs/demo-lab/runs" && method === "POST") {
        if (!req.headers["idempotency-key"]) return send(res, 400, { detail: "Idempotency-Key required" });
        const prior = idempotency.get(req.headers["idempotency-key"]);
        if (prior) return send(res, 202, { id: prior, run_id: prior, state: runs.get(prior).state, lab_id: "demo-lab" });
        const body = await read(req);
        const id = `demo-run-${runs.size + 1}`;
        const run = { id, lab_id: "demo-lab", state: "running", goal: body.goal, budget: body.budget, skill_packs: body.skill_packs, inputs: body.inputs, events: [] };
        runs.set(id, run);
        idempotency.set(req.headers["idempotency-key"], id);
        event(run, "run.state", { from: "queued", to: "running", reason: "demo fixture" });
        setTimeout(() => {
          if (run.state !== "running") return;
          event(run, "tool.finished", { tool: "demo.search", args_redacted: {}, duration_ms: 100, ok: true, error: null });
          event(run, "cost.updated", { tokens_in: 120, tokens_out: 60, llm_cost_thb: 1.25, compute_cost_thb: 0, budget_remaining_thb: null });
          run.state = "awaiting_approval";
          event(run, "approval.required", { approval_id: "demo-approval", action: "demo.report", reason: "ยืนยันก่อนสร้างรายงานตัวอย่าง", policy_rule: "demo", expires_at: new Date(Date.now() + 3600000).toISOString(), preview: {}, ts: new Date().toISOString() });
        }, 300);
        return send(res, 202, { id, run_id: id, state: run.state, lab_id: "demo-lab" });
      }
      if (path === "/v1/labs/demo-lab/runs" && method === "GET") return send(res, 200, [...runs.values()].map(({ events, ...run }) => run));
      const runMatch = /^\/v1\/runs\/([^/]+)(?:\/(.*))?$/.exec(path);
      if (runMatch) {
        const run = runs.get(runMatch[1]);
        if (!run) return send(res, 404, { detail: "run not found" });
        const tail = runMatch[2] || "";
        if (!tail && method === "GET") {
          const { events, ...visible } = run;
          return send(res, 200, visible);
        }
        if (tail === "events" && method === "GET") {
          const from = Number(url.searchParams.get("from_seq") || 0);
          res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
          for (const item of run.events.filter((item) => item.seq > from)) res.write(`id: ${item.seq}\nevent: ${item.type}\ndata: ${JSON.stringify(item)}\n\n`);
          const listeners = streams.get(run.id) || new Set();
          listeners.add(res);
          streams.set(run.id, listeners);
          req.on("close", () => listeners.delete(res));
          return;
        }
        if (tail === "artifacts" && method === "GET") return send(res, 200, run.state === "completed" ? [
          { id: "demo-report", kind: "report", metadata: { name: "รายงานตัวอย่าง.md", content_type: "text/markdown" } },
          { id: "demo-manifest", kind: "manifest", metadata: { name: "manifest.json", content_type: "application/json" } },
        ] : []);
        if (tail === "approvals/demo-approval" && method === "POST") {
          const body = await read(req);
          if (run.state !== "awaiting_approval") return send(res, 409, { detail: "approval already decided" });
          run.state = body.decision === "approve" ? "completed" : "cancelled";
          if (run.state === "completed") {
            event(run, "run.state", { from: "awaiting_approval", to: "running", reason: "approved" });
            event(run, "run.completed", { summary: "รายงานตัวอย่าง", report_artifact_id: "demo-report", manifest_artifact_id: "demo-manifest", claims_count: 1 });
          }
          else event(run, "run.state", { from: "awaiting_approval", to: "cancelled", reason: "approval_rejected" });
          return send(res, 200, { id: run.id, state: run.state, decision: body.decision });
        }
      }
      if (path === "/v1/artifacts/demo-report" && method === "GET") return send(res, 200, { id: "demo-report", kind: "report", sha256: "demo-only", metadata: { name: "รายงานตัวอย่าง.md", content_type: "text/markdown" }, url: "/v1/artifacts/demo-report/content" });
      if (path === "/v1/artifacts/demo-manifest" && method === "GET") return send(res, 200, { id: "demo-manifest", kind: "manifest", sha256: "demo-only", metadata: { name: "manifest.json", content_type: "application/json" }, url: "/v1/artifacts/demo-manifest/content" });
      if (path === "/v1/artifacts/demo-report/content" && method === "GET") {
        res.writeHead(200, { "content-type": "text/markdown; charset=utf-8" });
        return res.end("# รายงานตัวอย่าง\n\nข้อมูลจำลองเท่านั้น\n");
      }
      if (path === "/v1/artifacts/demo-manifest/content" && method === "GET") {
        return send(res, 200, { run_id: "demo-run", goal: "โจทย์ตัวอย่าง", claims: [{ id: "demo-claim", text: "ข้อสรุปตัวอย่าง", evidence: ["https://example.org/"], confidence: 0.5 }], demo: true });
      }
      if (path === "/v1/labs/demo-lab/members") {
        if (method === "GET") return send(res, 200, { members });
        if (method === "POST") {
          const body = await read(req);
          if (!body.subject || !["owner", "researcher", "viewer"].includes(body.role)) return send(res, 422, { detail: "invalid member" });
          if (members.some((m) => m.subject === body.subject)) return send(res, 409, { detail: "member exists" });
          const member = { subject: body.subject, role: body.role };
          members.push(member);
          return send(res, 201, member);
        }
      }
      const memberMatch = /^\/v1\/labs\/demo-lab\/members\/([^/]+)$/.exec(path);
      if (memberMatch) {
        const member = members.find((m) => m.subject === decodeURIComponent(memberMatch[1]));
        if (!member) return send(res, 404, { detail: "member not found" });
        if (member.role === "owner" && members.filter((m) => m.role === "owner").length === 1) return send(res, 409, { detail: "final owner protected" });
        if (method === "DELETE") {
          members.splice(members.indexOf(member), 1);
          res.writeHead(204);
          return res.end();
        }
        if (method === "PATCH") {
          const body = await read(req);
          if (!["owner", "researcher", "viewer"].includes(body.role)) return send(res, 422, { detail: "invalid role" });
          member.role = body.role;
          return send(res, 200, member);
        }
      }
      if (path === "/v1/labs/demo-lab/budget") {
        if (method === "GET") return send(res, 200, { budget_thb: budget === null ? null : String(budget) });
        if (method === "PUT") {
          const body = await read(req);
          if (body.budget_thb !== null && !(
            (typeof body.budget_thb === "number" && Number.isFinite(body.budget_thb) && body.budget_thb >= 0) ||
            (typeof body.budget_thb === "string" && /^(0|[1-9]\d*)(\.\d+)?$/.test(body.budget_thb))
          )) return send(res, 422, { detail: "invalid budget" });
          budget = body.budget_thb;
          return send(res, 200, { budget_thb: budget === null ? null : String(budget) });
        }
      }
      if (path === "/v1/labs/demo-lab/api-keys") {
        if (method === "GET") return send(res, 200, keys.map(({ secret, ...key }) => key));
        if (method === "POST") {
          const body = await read(req);
          const key = { id: `demo-key-${nextKey++}`, name: body.name || "Demo key", secret: "demo-secret-shown-once" };
          keys.push(key);
          return send(res, 200, key);
        }
        if (method === "DELETE") {
          const index = keys.findIndex((key) => key.id === url.searchParams.get("key_id"));
          if (index < 0) return send(res, 404, { detail: "key not found" });
          keys.splice(index, 1);
          res.writeHead(204);
          return res.end();
        }
      }
      if (path === "/v1/labs/demo-lab/peers") {
        if (method === "GET") return send(res, 200, peers);
        if (method === "POST") {
          const peer = { id: `demo-peer-${peers.length + 1}`, ...(await read(req)) };
          peers.push(peer);
          return send(res, 200, peer);
        }
      }
      if (path === "/v1/labs/demo-lab/usage" && method === "GET") return send(res, 200, { period: url.searchParams.get("period"), cost_thb: 1.25, demo: true });
      return send(res, 404, { detail: "demo route not found" });
    } catch (error) {
      return send(res, 400, { detail: error instanceof Error ? error.message : "invalid request" });
    }
  });
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  createDemoServer().listen(8787, "127.0.0.1", () => console.log("Demo fixture API on http://127.0.0.1:8787"));
}
