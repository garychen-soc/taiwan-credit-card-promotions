const test = require("node:test");
const assert = require("node:assert/strict");
const { deriveSourceHealth } = require("../docs/assets/source-health-state.js");

test("reports the website timestamp and every incomplete source timestamp", () => {
  const result = deriveSourceHealth({
    generated_at: "2026-09-24T10:00:00+08:00",
    source_health: [
      { id: "ok", bank_name: "正常銀行", status: "complete", last_updated_at: "2026-09-24T10:00:00+08:00" },
      { id: "partial", bank_name: "部分銀行", status: "partial", last_updated_at: "2026-09-23T18:30:00+08:00" },
      { id: "failed", bank_name: "失敗銀行", status: "failed", last_updated_at: "2026-09-20T01:00:00+08:00" }
    ]
  });

  assert.equal(result.websiteUpdatedAt, "2026-09-24T10:00:00+08:00");
  assert.equal(result.allComplete, false);
  assert.equal(result.anyFailed, true);
  assert.deepEqual(result.incomplete.map((item) => item.bankName), ["部分銀行", "失敗銀行"]);
  assert.deepEqual(result.incomplete.map((item) => item.lastUpdatedAt), [
    "2026-09-23T18:30:00+08:00",
    "2026-09-20T01:00:00+08:00"
  ]);
});

test("does not substitute an attempted check time for an unknown update time", () => {
  const result = deriveSourceHealth({
    generated_at: "2026-09-24T10:00:00+08:00",
    source_health: [{
      id: "partial",
      bank_name: "部分銀行",
      status: "partial",
      checked_at: "2026-09-24T10:00:00+08:00"
    }]
  });

  assert.equal(result.incomplete[0].lastUpdatedAt, "");
});
