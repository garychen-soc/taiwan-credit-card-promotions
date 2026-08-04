const test = require("node:test");
const assert = require("node:assert/strict");
const { deriveActivity, lifecycleFor } = require("../docs/assets/time-state.js");

test("lifecycle follows the current Taipei date instead of generated data", () => {
  const activity = { start_date: "2026-08-03", end_date: "2026-08-10" };
  assert.equal(lifecycleFor(activity, new Date("2026-08-02T12:00:00+08:00")), "upcoming");
  assert.equal(lifecycleFor(activity, new Date("2026-08-04T12:00:00+08:00")), "active");
  assert.equal(lifecycleFor(activity, new Date("2026-08-11T00:00:00+08:00")), "ended");
});

test("high return is derived from artifact thresholds", () => {
  const activity = {
    start_date: "2026-08-01",
    end_date: "2026-08-31",
    max_reward_percent: 9,
    max_reward_amount_twd: 500,
    high_return: false,
    featured: false,
    registration_required: false
  };
  const derived = deriveActivity(
    activity,
    { percent_at_least: 10, amount_twd_at_least: 500 },
    new Date("2026-08-04T12:00:00+08:00")
  );
  assert.equal(derived.high_return, true);
  assert.equal(derived.featured, true);
  assert.equal(derived.lifecycle, "active");
});
