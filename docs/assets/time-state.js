(function attachTimeState(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CardPromotionTime = api;
}(typeof globalThis === "object" ? globalThis : this, () => {
  "use strict";

  function taipeiDateKey(date = new Date()) {
    return new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      timeZone: "Asia/Taipei"
    }).format(date);
  }

  function lifecycleFor(activity, now = new Date()) {
    const today = taipeiDateKey(now);
    if (activity.end_date && activity.end_date < today) return "ended";
    if (activity.start_date && activity.start_date > today) return "upcoming";
    return "active";
  }

  function highReturnFor(activity, thresholds) {
    const percent = Number(thresholds?.percent_at_least ?? 10);
    const amount = Number(thresholds?.amount_twd_at_least ?? 500);
    return (
      (activity.max_reward_percent != null && Number(activity.max_reward_percent) >= percent)
      || (activity.max_reward_amount_twd != null && Number(activity.max_reward_amount_twd) >= amount)
    );
  }

  function deriveActivity(activity, thresholds, now = new Date()) {
    const highReturn = highReturnFor(activity, thresholds);
    return {
      ...activity,
      lifecycle: lifecycleFor(activity, now),
      high_return: highReturn,
      featured: Boolean(activity.featured || activity.registration_required || highReturn)
    };
  }

  return { deriveActivity, highReturnFor, lifecycleFor, taipeiDateKey };
}));
