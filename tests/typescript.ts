import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { Pgxio, PgxioError } from "../sdk/typescript/src/index.ts";
import type { API, Streams } from "../examples/generated/contract.ts";

const token = () => execFileSync("python3", ["bin/pgxio", "token"], { encoding: "utf8" }).trim();
const client = new Pgxio<API, Streams>("http://127.0.0.1:8080", token);
assert(Array.isArray((await client.call("list_tasks", {})).tasks));
await assert.rejects(() => client.call("create_task", { title: "" }), PgxioError);
let snapshots = 0;
await new Promise<void>((resolve, reject) => {
  const timeout = setTimeout(() => { stop(); reject(new Error("subscription timed out")); }, 5000);
  const stop = client.watch("tasks", {}, {
    snapshot: signal => client.call("list_tasks", {}, signal),
    onData: value => {
      assert(Array.isArray(value.tasks));
      snapshots++;
      if (snapshots === 1) void client.call("create_task", { title: "typescript sdk" }).catch(reject);
      if (snapshots === 2) { clearTimeout(timeout); stop(); resolve(); }
    },
    onError: reject,
  });
});
console.log("TypeScript: HTTP, errors, subscription snapshot and refresh passed");
