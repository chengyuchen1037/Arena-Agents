// bench/judges/judge.workflow.js - the judge of the v0.14 benchmark, for DSH's `workflow` tool (docs/v0.14-plan.md H48).
//
// How to run it (the user does this in DSH after `python -m bench judge-pack --items judge_items.jsonl --out packs/`):
//   * script = the text of THIS file (the tool wants a plain JavaScript body: no `export`, no imports; it ends with `return`);
//   * meta   = {"name": "bench-judge", "description": "Judge the items of one benchmark pack with one sub-agent each.",
//               "phases": [{"title": "judge"}]};
//   * args   = {"pack": <the content of ONE packs/<rubric>-<NN>.json>, "concurrency": 2}   (a pack holds at most 40 items;
//               concurrency is optional, 1 to 3, default 2);
//   * the value it returns is a JSON object ({format: "arena-bench-verdicts", ...}) - save it to a file
//     (for example verdicts-L3-01.json) and merge it with `python -m bench judge-merge`.
//
// What it does: one sub-agent per item.  The prompt of an item is ALREADY complete in the pack (the instructions, the rubric text and the blind view of the
// item - no bot, no run, no truth): this script adds nothing to it and reads nothing else.  The sub-agent answers with a JSON object that must fit the
// rubric's schema (the tool checks it); a sub-agent that fails or returns nothing is asked once more; an item that still has no verdict is listed in
// `errors`, never invented.  The judge is a DSH sub-agent - never the model that plays.  It uses only the hooks the workflow tool documents:
// agent, parallel, phase, log, args.
//
// Capacity: DSH allows only a few sub-agents at a time (the "active child limit", 3 by default) and the limit is SHARED with everything else that runs
// in the session - teammates included.  The items are therefore dealt out to `concurrency` lanes that each judge one item after the other, so the script
// never asks for more children than that; if DSH still answers "subagent limit reached", wait for the other agents to finish (or pass concurrency: 1) and
// run the pack again - the items that got a verdict are in the result, the others are in `errors` (with the reason in `notes`).

const pack = args && args.pack;
if (!pack || pack.format !== "arena-bench-judge-pack" || !Array.isArray(pack.items) || !pack.schema) {
  throw new Error("args.pack must be one pack written by `python -m bench judge-pack`");
}
if (pack.items.length > 40) {
  throw new Error("a pack holds at most 40 items (got " + pack.items.length + "): pack it again with --batch 40");
}
const requested = Number(args.concurrency);
const lanes = Math.max(1, Math.min(3, Number.isFinite(requested) && requested >= 1 ? Math.floor(requested) : 2));
const TRIES = 2;

phase("judge");
log("rubric " + pack.rubric + " v" + pack.rubric_version + ", batch " + pack.batch + "/" + pack.batches + ": " + pack.items.length + " items, " + lanes + " at a time");

const verdictOf = {};
const notes = [];

async function judgeOne(item) {
  for (let attempt = 1; attempt <= TRIES; attempt++) {
    let verdict = null;
    try {
      verdict = await agent(item.prompt, {
        schema: pack.schema,
        label: pack.rubric + " " + item.blind_id,
        phase: "judge",
      });
    } catch (error) {
      notes.push(item.blind_id + ": " + String(error && error.message ? error.message : error).slice(0, 200));
      return;
    }
    if (verdict !== null && verdict !== undefined) {
      verdictOf[item.blind_id] = { blind_id: item.blind_id, verdict: verdict, attempts: attempt };
      return;
    }
  }
}

const lane = [];
for (let i = 0; i < lanes; i++) {
  lane.push([]);
}
pack.items.forEach((item, index) => lane[index % lanes].push(item));
await parallel(
  lane.map((items) => async () => {
    for (const item of items) {
      await judgeOne(item);
    }
  })
);

const verdicts = [];
const errors = [];
for (const item of pack.items) {
  if (verdictOf[item.blind_id]) {
    verdicts.push(verdictOf[item.blind_id]);
  } else {
    errors.push(item.blind_id);
  }
}
log(verdicts.length + " verdicts, " + errors.length + " without one");

return {
  format: "arena-bench-verdicts",
  rubric: pack.rubric,
  rubric_version: pack.rubric_version,
  batch: pack.batch,
  verdicts: verdicts,
  errors: errors,
  notes: notes,
};
