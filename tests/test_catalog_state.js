const test = require("node:test");
const assert = require("node:assert/strict");
const catalog = require("../docs/assets/catalog-state.js");

test("fixed pagination never appends more than 50 activities", () => {
  const values = Array.from({ length: 1113 }, (_, index) => index);
  const middle = catalog.paginate(values, "all", 2);
  const last = catalog.paginate(values, "all", 99);
  assert.equal(middle.items.length, 50);
  assert.equal(middle.items[0], 50);
  assert.equal(last.page, 23);
  assert.equal(last.items.length, 13);
});

test("today tomorrow and review views stay unpaginated", () => {
  const values = Array.from({ length: 75 }, (_, index) => index);
  for (const filter of ["today", "tomorrow", "review"]) {
    const result = catalog.paginate(values, filter, 2);
    assert.equal(result.paginated, false);
    assert.equal(result.items.length, 75);
  }
});

test("catalog state round trips through a shareable URL", () => {
  const state = catalog.read("?filter=all&bank=esun&category=%E7%B6%B2%E8%B3%BC&q=momo&sort=ending&page=2");
  assert.deepEqual(state, { filter: "all", bank: "esun", category: "網購", query: "momo", sort: "ending", page: 2 });
  assert.equal(catalog.read(catalog.search(state)).page, 2);
});
