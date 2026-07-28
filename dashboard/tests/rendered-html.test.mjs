import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the private paper-operations shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Stage 1 Paper Operations<\/title>/i);
  assert.match(html, /Connecting to the local Stage 1 control room/);
  assert.doesNotMatch(html, /FYERS_ACCESS_TOKEN|TELEGRAM_BOT_TOKEN|\.env/);
});

test("client is a read-only telemetry surface", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  const bridge = await readFile(
    new URL("../../scripts/run_control_room.py", import.meta.url),
    "utf8",
  );

  assert.match(page, /PAPER ONLY/);
  assert.match(page, /Broker orders impossible/);
  assert.match(page, /Read-only local telemetry/);
  assert.match(bridge, /strictly read-only/);
  assert.match(bridge, /do_POST/);
  assert.doesNotMatch(bridge, /place_order|orderSocket|order_ws/);
  assert.doesNotMatch(page, /fetch\([^)]*\{[^}]*method:\s*["']POST/i);
});
