(function attachCatalogState(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CardPromotionCatalog = api;
}(typeof globalThis === "object" ? globalThis : this, () => {
  "use strict";
  const PAGE_SIZE = 50;
  const FILTERS = new Set(["priority", "today", "tomorrow", "review", "registration", "high-return", "upcoming", "ending", "all"]);
  const UNPAGINATED = new Set(["today", "tomorrow", "review"]);

  function read(search) {
    const params = new URLSearchParams(search || "");
    const filter = params.get("filter") || "priority";
    return {
      filter: FILTERS.has(filter) ? filter : "priority",
      bank: params.get("bank") || "",
      category: params.get("category") || "",
      query: params.get("q") || "",
      sort: params.get("sort") || "recommended",
      page: Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1)
    };
  }

  function search(state) {
    const params = new URLSearchParams();
    params.set("filter", state.filter || "priority");
    if (state.bank) params.set("bank", state.bank);
    if (state.category) params.set("category", state.category);
    if (state.query) params.set("q", state.query);
    if (state.sort && state.sort !== "recommended") params.set("sort", state.sort);
    if (state.page > 1 && !UNPAGINATED.has(state.filter)) params.set("page", String(state.page));
    return `?${params.toString()}`;
  }

  function paginate(values, filter, requestedPage) {
    if (UNPAGINATED.has(filter)) {
      return { items: values, page: 1, pageCount: 1, paginated: false };
    }
    const pageCount = Math.max(1, Math.ceil(values.length / PAGE_SIZE));
    const page = Math.min(Math.max(1, requestedPage || 1), pageCount);
    const start = (page - 1) * PAGE_SIZE;
    return { items: values.slice(start, start + PAGE_SIZE), page, pageCount, paginated: pageCount > 1 };
  }

  return { PAGE_SIZE, paginate, read, search };
}));
