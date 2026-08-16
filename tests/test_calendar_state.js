const test = require("node:test");
const assert = require("node:assert/strict");
const { escapeIcs, foldIcsLine, serializeIcs } = require("../docs/assets/calendar-state.js");

test("ICS text escapes separators without losing the backslash", () => {
  assert.equal(escapeIcs("銀行;活動,提醒\\路徑\n下一行"), "銀行\\;活動\\,提醒\\\\路徑\\n下一行");
});

test("UTF-8 content lines fold at 75 octets with continuation spaces", () => {
  const logical = `DESCRIPTION:${escapeIcs("中文活動；".repeat(30))}`;
  const folded = foldIcsLine(logical);
  const physical = folded.split("\r\n");

  assert.ok(physical.length > 1);
  assert.ok(physical.slice(1).every((line) => line.startsWith(" ")));
  assert.ok(physical.every((line) => Buffer.byteLength(line, "utf8") <= 75));
  assert.equal(physical[0] + physical.slice(1).map((line) => line.slice(1)).join(""), logical);
});

test("serialized calendar uses CRLF and keeps the terminating blank line", () => {
  const value = serializeIcs(["BEGIN:VCALENDAR", "END:VCALENDAR", ""]);
  assert.equal(value, "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n");
});
