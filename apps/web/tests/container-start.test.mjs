import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

test("production Web start listens on every container interface", () => {
  assert.match(pkg.scripts.start, /(?:--hostname|-H)\s+0\.0\.0\.0(?:\s|$)/);
});
