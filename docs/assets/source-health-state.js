(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CardPromotionSourceHealth = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function deriveSourceHealth(data) {
    const health = Array.isArray(data && data.source_health) ? data.source_health : [];
    const incomplete = health
      .filter((item) => item && item.status !== "complete")
      .map((item) => ({
        id: String(item.id || ""),
        bankName: String(item.bank_name || item.id || "未知來源"),
        status: item.status === "failed" ? "failed" : "partial",
        lastUpdatedAt: String(item.last_updated_at || "")
      }));
    return {
      websiteUpdatedAt: String((data && data.generated_at) || ""),
      allComplete: health.length > 0 && incomplete.length === 0,
      anyFailed: incomplete.some((item) => item.status === "failed"),
      incomplete
    };
  }

  return { deriveSourceHealth };
});
