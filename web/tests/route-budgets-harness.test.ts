import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

// `scripts/route_budgets.mjs` is an ES module and this suite compiles to
// CommonJS, so the exported predicates are exercised in an ESM child process —
// the same boundary `build-wrapper.test.ts` uses for `scripts/build.mjs`.
const webRoot = process.cwd();
const scriptUrl = pathToFileURL(
  path.join(webRoot, "scripts", "route_budgets.mjs"),
).href;

const read = (...parts: string[]) =>
  readFileSync(path.join(webRoot, ...parts), "utf8");

function runEsm(body: string): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync(
    process.execPath,
    ["--input-type=module", "-e", body],
    { cwd: webRoot, encoding: "utf8" },
  );
  return {
    status: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
  };
}

/** Shared assertion helper used inside the ESM child process. */
const throwsWith = `
const throwsWith = (fn, fragment) => {
  let message = "";
  try {
    fn();
  } catch (error) {
    message = error instanceof Error ? error.message : String(error);
  }
  assert.notEqual(message, "", "expected a failure, but nothing was thrown");
  assert.ok(
    message.includes(fragment),
    "expected message to include " + JSON.stringify(fragment) + " but got " + JSON.stringify(message),
  );
};
`;

test("a route budget is rejected when the measurement followed a redirect", () => {
  // `/` is a 307 to `/chat`; following it sized the chat payload against the
  // `/` budget. The harness must refuse such a measurement instead of reporting it.
  const result = runEsm(`
import assert from "node:assert/strict";
import { assertDirectRouteMeasurement } from ${JSON.stringify(scriptUrl)};
${throwsWith}

throwsWith(
  () =>
    assertDirectRouteMeasurement("/", {
      redirected: true,
      url: "http://127.0.0.1:3000/chat",
      ok: true,
      status: 200,
    }),
  "redirected to /chat during route measurement",
);

// A direct 200 is the only measurement that is meaningful.
assert.doesNotThrow(() =>
  assertDirectRouteMeasurement("/chat/perf-budget", {
    redirected: false,
    url: "http://127.0.0.1:3000/chat/perf-budget",
    ok: true,
    status: 200,
  }),
);

// Non-OK responses keep failing.
throwsWith(
  () =>
    assertDirectRouteMeasurement("/settings", {
      redirected: false,
      url: "http://127.0.0.1:3000/settings",
      ok: false,
      status: 500,
    }),
  "returned HTTP 500",
);
console.log("direct-measurement-guard-ok");
`);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /direct-measurement-guard-ok/);
});

test("the intended redirect is asserted rather than measured", () => {
  const result = runEsm(`
import assert from "node:assert/strict";
import { assertExpectedRedirect } from ${JSON.stringify(scriptUrl)};
${throwsWith}

assert.doesNotThrow(() =>
  assertExpectedRedirect("/", "/chat", {
    redirected: true,
    url: "http://127.0.0.1:3000/chat",
    ok: true,
    status: 200,
  }),
);

// Silently losing the redirect must fail loudly.
throwsWith(
  () =>
    assertExpectedRedirect("/", "/chat", {
      redirected: false,
      url: "http://127.0.0.1:3000/",
      ok: true,
      status: 200,
    }),
  "expected to redirect to /chat",
);

// Redirecting somewhere else must fail loudly too.
throwsWith(
  () =>
    assertExpectedRedirect("/", "/chat", {
      redirected: true,
      url: "http://127.0.0.1:3000/login",
      ok: true,
      status: 200,
    }),
  "expected to redirect to /chat",
);
console.log("redirect-assertion-ok");
`);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /redirect-assertion-ok/);
});

test("the redirect-only route is excluded from the budgeted rows", () => {
  // Guards the failure the harness fix removes: `/` must not reappear as a
  // measured row, because its payload belongs to the chat budget.
  const result = runEsm(`
import { ROUTE_TARGETS, REDIRECT_TARGETS } from ${JSON.stringify(scriptUrl)};

const measured = ROUTE_TARGETS.map((row) => row.requestPath);
const redirected = REDIRECT_TARGETS.map((row) => row.requestPath);

console.log(JSON.stringify({ measured, redirected }));
`);
  assert.equal(result.status, 0, result.stderr);
  const parsed = JSON.parse(result.stdout.trim()) as {
    measured: string[];
    redirected: string[];
  };
  assert.ok(
    !parsed.measured.includes("/"),
    `"/" must not be measured directly; it redirects to /chat. Got ${JSON.stringify(parsed.measured)}`,
  );
  assert.deepEqual(parsed.redirected, ["/"]);
  assert.ok(
    parsed.measured.includes("/chat/perf-budget"),
    "the chat payload must still be budgeted under /chat/[sessionId]",
  );
});

test("importing the harness does not launch a production server", () => {
  // The module is imported by this suite, so running `main()` on import would
  // start a server and measure routes during a unit test.
  const source = read("scripts", "route_budgets.mjs");
  assert.match(source, /const invokedDirectly =/);
  assert.match(source, /if \(invokedDirectly\) \{/);
});
