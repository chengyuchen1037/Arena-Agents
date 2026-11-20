// Runs bench/judges/judge.workflow.js the way DSH's workflow tool does - a plain JavaScript body with the hooks agent / parallel / phase / log and `args`
// in scope, top-level `await` and `return` allowed - against a scripted stand-in for the sub-agents.  Used by tests/bench/test_judging.py:
//     node judge_workflow_harness.js <script file>        (the scenario is JSON on stdin)
// scenario = {args: {...}, agent: "ok" | "null-once" | "null" | "throw" | "throw-once", maxActive?: n}
// prints {result, calls, maxActive, logs, phases} as JSON; a thrown error is printed as {error}.

const fs = require("fs");

const scriptPath = process.argv[2];
const scenario = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(scriptPath, "utf8");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const calls = [];
const logs = [];
const phases = [];
let active = 0;
let maxActive = 0;
const seen = {};

async function agent(prompt, opts) {
  calls.push({ prompt, schema: opts && opts.schema, label: opts && opts.label });
  active += 1;
  maxActive = Math.max(maxActive, active);
  try {
    await new Promise((resolve) => setImmediate(resolve));
    if (scenario.maxActive && active > scenario.maxActive) {
      throw new Error("subagent limit reached (active child limit: " + scenario.maxActive + ")");
    }
    const key = opts && opts.label;
    seen[key] = (seen[key] || 0) + 1;
    const mode = scenario.agent;
    if (mode === "null") return null;
    if (mode === "null-once" && seen[key] === 1) return null;
    if (mode === "throw") throw new Error("boom " + key);
    if (mode === "throw-once" && seen[key] === 1) throw new Error("boom " + key);
    return { basis: "decisive_fact", cites_decisive: true, reason: "ok " + key };
  } finally {
    active -= 1;
  }
}

async function parallel(thunks) {
  return Promise.all(thunks.map(async (thunk) => {
    try {
      return await thunk();
    } catch (error) {
      return null;
    }
  }));
}

async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, index) => {
    try {
      let prev = item;
      for (const stage of stages) prev = await stage(prev, item, index);
      return prev;
    } catch (error) {
      return null;
    }
  }));
}

(async () => {
  try {
    const run = new AsyncFunction("args", "agent", "parallel", "pipeline", "phase", "log", source);
    const result = await run(scenario.args, agent, parallel, pipeline, (title) => phases.push(title), (message) => logs.push(message));
    console.log(JSON.stringify({ result, calls, maxActive, logs, phases }));
  } catch (error) {
    console.log(JSON.stringify({ error: String(error && error.message ? error.message : error), calls, maxActive, logs, phases }));
  }
})();
