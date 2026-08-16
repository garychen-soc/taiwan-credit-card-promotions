(() => {
  "use strict";

  const DATA_URL = "./data/promotions.json";
  const DAY_MS = 86400000;
  const MINUTE_MS = 60000;
  const REGISTRATION_CALENDAR_DURATION_MINUTES = 15;
  const INDEX_FILTERS = new Set(["today", "tomorrow", "registration"]);
  const timeState = window.CardPromotionTime;
  const catalogState = window.CardPromotionCatalog;
  const { escapeIcs, serializeIcs } = window.CardPromotionCalendar;
  const state = {
    data: null,
    activities: [],
    filter: "registration",
    bank: "",
    category: "",
    query: "",
    sort: "recommended",
    page: 1,
    agendaDate: "",
    catalogLoaded: false,
    catalogPromise: null,
    detailCache: new Map()
  };

  const el = {
    sourceDot: document.querySelector("#source-dot"),
    updateLabel: document.querySelector("#update-label"),
    statRegistration: document.querySelector("#stat-registration"),
    statHighReturn: document.querySelector("#stat-high-return"),
    statTotal: document.querySelector("#stat-total"),
    thresholdNote: document.querySelector("#threshold-note"),
    todayDate: document.querySelector("#today-date"),
    tomorrowDate: document.querySelector("#tomorrow-date"),
    todayCount: document.querySelector("#today-count"),
    tomorrowCount: document.querySelector("#tomorrow-count"),
    todayAgenda: document.querySelector("#today-agenda"),
    tomorrowAgenda: document.querySelector("#tomorrow-agenda"),
    quickFilters: document.querySelector("#quick-filters"),
    searchInput: document.querySelector("#search-input"),
    bankSelect: document.querySelector("#bank-select"),
    categorySelect: document.querySelector("#category-select"),
    sortSelect: document.querySelector("#sort-select"),
    resultCount: document.querySelector("#result-count"),
    activityList: document.querySelector("#activity-list"),
    pagination: document.querySelector("#pagination"),
    pagePrevious: document.querySelector("#page-previous"),
    pageNext: document.querySelector("#page-next"),
    pageStatus: document.querySelector("#page-status"),
    emptyState: document.querySelector("#empty-state"),
    clearFilters: document.querySelectorAll("[data-clear-filters]"),
    sourceHealthList: document.querySelector("#source-health-list"),
    alertsPanel: document.querySelector("#alerts-panel"),
    agendaTemplate: document.querySelector("#agenda-item-template"),
    activityTemplate: document.querySelector("#activity-card-template")
  };

  const dateFmt = new Intl.DateTimeFormat("zh-TW", {
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "Asia/Taipei"
  });
  const shortDateFmt = new Intl.DateTimeFormat("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Asia/Taipei"
  });
  const timeFmt = new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Taipei"
  });

  function parseDate(value, endOfDay = false) {
    if (!value) return null;
    const text = String(value);
    const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(text)
      ? `${text}T${endOfDay ? "23:59:59" : "00:00:00"}+08:00`
      : text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function taipeiDateKey(date = new Date()) {
    return new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      timeZone: "Asia/Taipei"
    }).format(date);
  }

  function addDays(key, days) {
    const date = parseDate(key);
    return taipeiDateKey(new Date(date.getTime() + days * DAY_MS));
  }

  function addMinutes(value, minutes) {
    const date = parseDate(value);
    if (!date) return value;
    return new Date(date.getTime() + minutes * MINUTE_MS).toISOString();
  }

  function resolveDataUrl(reference) {
    return new URL(reference, new URL(DATA_URL, window.location.href)).href;
  }

  function utcIcs(value) {
    return new Date(value).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  }

  function dateIcs(value) {
    return String(value).replaceAll("-", "");
  }

  function googleCalendarUrl({ title, start, end, details, allDay = false }) {
    const params = new URLSearchParams({
      action: "TEMPLATE",
      text: title,
      details,
      ctz: "Asia/Taipei"
    });
    if (allDay) {
      const exclusiveEnd = end ? addDays(end, 1) : addDays(start, 1);
      params.set("dates", `${dateIcs(start)}/${dateIcs(exclusiveEnd)}`);
    } else {
      params.set("dates", `${utcIcs(start)}/${utcIcs(end)}`);
    }
    return `https://calendar.google.com/calendar/render?${params}`;
  }

  function downloadIcs(config) {
    const uid = `${Date.now()}-${Math.random().toString(16).slice(2)}@card-promotion-radar`;
    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//Card Promotion Radar//ZH-TW",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "BEGIN:VEVENT",
      `UID:${uid}`,
      `DTSTAMP:${utcIcs(new Date().toISOString())}`,
      `SUMMARY:${escapeIcs(config.title)}`,
      `DESCRIPTION:${escapeIcs(config.details)}`,
      `URL:${escapeIcs(config.url || "")}`
    ];
    if (config.allDay) {
      lines.push(`DTSTART;VALUE=DATE:${dateIcs(config.start)}`);
      lines.push(`DTEND;VALUE=DATE:${dateIcs(addDays(config.end || config.start, 1))}`);
    } else {
      lines.push(`DTSTART:${utcIcs(config.start)}`);
      lines.push(`DTEND:${utcIcs(config.end)}`);
      lines.push("BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY", `DESCRIPTION:${escapeIcs(config.title)}`, "END:VALARM");
    }
    lines.push("END:VEVENT", "END:VCALENDAR", "");
    const blob = new Blob([serializeIcs(lines)], { type: "text/calendar;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${config.filename || "calendar"}.ics`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  function calendarConfigForWindow(activity, window) {
    return {
      title: `[登錄] ${activity.bank_name}｜${activity.title}`,
      start: window.start,
      end: addMinutes(window.start, REGISTRATION_CALENDAR_DURATION_MINUTES),
      details: `${window.label}\n${window.source_text || ""}\n\n官方活動頁：${activity.source_url}`,
      url: activity.registration_url || activity.source_url,
      filename: `${activity.bank_name}-${activity.merchant}-登錄提醒`
    };
  }

  function registrationLinkText(activity) {
    if (activity.registration_url_kind === "bank_portal") return "前往統一登錄頁";
    if (activity.registration_url_kind === "activity_specific") return "前往活動登錄";
    return activity.bank_id === "dbs" ? "查看登錄方式" : "前往登錄";
  }

  function appendPortalHint(target, activity) {
    if (activity.registration_url_kind !== "bank_portal") return;
    const hint = document.createElement("p");
    hint.className = "registration-portal-hint";
    hint.textContent = `此為統一登錄頁，到站後請找「${activity.title}」。`;
    target.append(hint);
  }

  function appendTimingContracts(target, activity) {
    const messages = timeState.registrationTimingMessages(
      activity.registration_timing_contracts
    );
    const section = document.createElement("section");
    section.className = "registration-timing";
    const heading = document.createElement("strong");
    heading.textContent = "登錄與消費順序";
    const list = document.createElement("ul");
    messages.forEach((message) => {
      const item = document.createElement("li");
      item.textContent = message;
      list.append(item);
    });
    section.append(heading, list);
    target.append(section);
  }

  function calendarConfigForActivity(activity) {
    return {
      title: `${activity.bank_name}｜${activity.title}`,
      start: activity.start_date,
      end: activity.end_date || activity.start_date,
      details: `${activity.summary}\n\n官方活動頁：${activity.source_url}\n實際資格與名額以官方最新公告為準。`,
      url: activity.source_url,
      filename: `${activity.bank_name}-${activity.merchant}-活動期間`,
      allDay: true
    };
  }

  function setIcsButton(button, config) {
    button.addEventListener("click", () => downloadIcs(config));
  }

  function windowStatus(window) {
    const now = new Date();
    const start = parseDate(window.start);
    const end = parseDate(window.end);
    if (end && end < now) return "已結束";
    if (start && start <= now && !end) return "已開放";
    if (start && start <= now && (!end || end >= now)) return "登錄中";
    return "即將開放";
  }

  function renderAgendaColumn(target, dateKey, items, countTarget) {
    target.replaceChildren();
    countTarget.textContent = items.length;
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "agenda-empty";
      empty.innerHTML = "<strong>目前沒有登錄時點</strong><p>仍可從下方查看所有需登錄活動。</p>";
      target.append(empty);
      return;
    }
    items.forEach((item) => {
      const activity = state.activities.find((value) => value.id === item.activity_id);
      if (!activity) return;
      const node = el.agendaTemplate.content.cloneNode(true);
      const article = node.querySelector(".agenda-item");
      const status = windowStatus(item);
      if (status === "已結束") article.classList.add("is-past");
      node.querySelector(".agenda-time strong").textContent = timeFmt.format(parseDate(item.start));
      node.querySelector(".agenda-time span").textContent = status;
      node.querySelector(".agenda-bank").textContent = `${item.bank_name} · ${item.merchant}`;
      node.querySelector("h4").textContent = item.title;
      const register = node.querySelector(".agenda-register");
      register.href = item.registration_url || item.source_url;
      register.textContent = registrationLinkText(activity);
      appendPortalHint(node.querySelector(".agenda-content"), activity);
      const config = calendarConfigForWindow(activity, item);
      const google = node.querySelector(".agenda-google");
      google.href = googleCalendarUrl(config);
      setIcsButton(node.querySelector(".ics-button"), config);
      target.append(node);
    });
  }

  function agendaItemsFor(dateKey) {
    const values = [];
    state.activities.forEach((activity) => {
      (activity.registration_windows || []).forEach((window) => {
        if (!window.start || !window.start.startsWith(dateKey)) return;
        values.push({
          ...window,
          activity_id: activity.id,
          bank_name: activity.bank_name,
          title: activity.title,
          merchant: activity.merchant,
          registration_url: activity.registration_url,
          source_url: activity.source_url
        });
      });
    });
    return values.sort((a, b) => (
      a.start.localeCompare(b.start)
      || a.bank_name.localeCompare(b.bank_name, "zh-Hant")
      || a.title.localeCompare(b.title, "zh-Hant")
    ));
  }

  function renderAgenda() {
    const today = taipeiDateKey();
    const tomorrow = addDays(today, 1);
    state.agendaDate = today;
    el.todayDate.textContent = dateFmt.format(parseDate(today));
    el.tomorrowDate.textContent = dateFmt.format(parseDate(tomorrow));
    renderAgendaColumn(el.todayAgenda, today, agendaItemsFor(today), el.todayCount);
    renderAgendaColumn(el.tomorrowAgenda, tomorrow, agendaItemsFor(tomorrow), el.tomorrowCount);
  }

  function refreshDateSensitiveViews() {
    if (!state.data || state.agendaDate === taipeiDateKey()) return;
    state.activities = state.activities.map((activity) => (
      timeState.deriveActivity(activity, state.data.thresholds)
    ));
    updateDerivedSummary();
    renderAgenda();
    renderActivities();
  }

  function updateDerivedSummary() {
    if (!state.catalogLoaded && state.data?.catalog) {
      el.statRegistration.textContent = state.data.summary.registration_required;
      el.statHighReturn.textContent = state.data.summary.high_return;
      el.statTotal.textContent = state.data.catalog.activity_count;
      return;
    }
    const current = state.activities.filter((activity) => activity.lifecycle !== "ended");
    el.statRegistration.textContent = current.filter((activity) => activity.registration_required).length;
    el.statHighReturn.textContent = current.filter((activity) => activity.high_return).length;
    el.statTotal.textContent = current.length;
  }

  function formatPeriod(activity) {
    if ((activity.activity_periods || []).length > 1) {
      return activity.activity_periods.map((period) => (
        `${period.label} ${shortDateFmt.format(parseDate(period.start))}－${shortDateFmt.format(parseDate(period.end, true))}`
      )).join("；");
    }
    const start = shortDateFmt.format(parseDate(activity.start_date));
    if (!activity.end_date) return `${start} 起`;
    return `${start}－${shortDateFmt.format(parseDate(activity.end_date, true))}`;
  }

  function rewardLabel(activity) {
    const values = [];
    if (activity.max_reward_percent != null) values.push(`${activity.max_reward_percent}%`);
    if (activity.max_reward_amount_twd != null) {
      values.push(`NT$${Number(activity.max_reward_amount_twd).toLocaleString("zh-TW")}`);
    }
    return values.length ? values.join(" / ") : "依活動辦法";
  }

  function statusBadges(activity) {
    const badges = [];
    if (activity.registration_required) badges.push(["重點登錄", "is-registration"]);
    if (activity.high_return) badges.push(["高回饋", "is-high"]);
    if (activity.needs_review) badges.push(["需人工確認", "is-review"]);
    if (activity.lifecycle === "upcoming") badges.push(["即將開始", "is-upcoming"]);
    if (activity.lifecycle === "ended") badges.push(["官方已結束", "is-ended"]);
    return badges;
  }

  function relevantWindows(activity) {
    const now = Date.now();
    const future = activity.registration_windows.filter((item) => {
      const boundary = parseDate(item.end) || parseDate(item.start);
      return boundary && boundary.getTime() >= now;
    });
    return (future.length ? future : activity.registration_windows.slice(-1)).slice(0, 3);
  }

  const termsLabels = {
    period: "活動期間",
    eligibility: "參加資格",
    offer: "優惠內容",
    method: "活動辦法",
    registration: "登錄辦法",
    installment: "分期辦法",
    quota: "名額／限量",
    notes: "注意事項",
    overview: "其他條件"
  };

  function appendTermsSections(details, sections) {
    const entries = Object.entries(sections || {}).filter(([, value]) => value);
    const content = document.createElement("div");
    content.className = "terms-content";
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.textContent = "目前沒有可分段顯示的條款，請查看官方活動頁。";
      content.append(empty);
    }
    entries.forEach(([key, value]) => {
      const section = document.createElement("section");
      const heading = document.createElement("h4");
      heading.textContent = termsLabels[key] || key;
      const paragraph = document.createElement("p");
      paragraph.textContent = value;
      section.append(heading, paragraph);
      content.append(section);
    });
    details.append(content);
  }

  function appendTermsDetails(cardBody, activity) {
    const inlineSections = activity.terms_sections || {};
    if (!activity.detail_ref && !Object.keys(inlineSections).length) return;
    const details = document.createElement("details");
    details.className = "terms-details";
    const summary = document.createElement("summary");
    summary.textContent = "查看活動條件";
    details.append(summary);
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.rendered === "true") return;
      details.dataset.rendered = "true";
      if (Object.keys(inlineSections).length) {
        appendTermsSections(details, inlineSections);
        return;
      }
      const loading = document.createElement("p");
      loading.className = "terms-loading";
      loading.textContent = "正在載入活動條款…";
      details.append(loading);
      try {
        let detail = state.detailCache.get(activity.id);
        if (!detail) {
          const response = await fetch(resolveDataUrl(activity.detail_ref), { cache: "no-store" });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          detail = await response.json();
          state.detailCache.set(activity.id, detail);
        }
        loading.remove();
        appendTermsSections(details, detail.terms_sections);
      } catch (error) {
        loading.textContent = "活動條款暫時無法載入，請改看官方活動頁。";
        console.error(error);
      }
    });
    cardBody.append(details);
  }

  function appendReviewAndTiers(cardBody, activity) {
    const reviewShownInRegistration = (
      activity.registration_required
      && !(activity.registration_windows || []).length
    );
    if (activity.needs_review && !reviewShownInRegistration) {
      const review = document.createElement("p");
      review.className = "data-review";
      review.textContent = activity.review_message || "本頁含多個活動，請至官方頁確認對應的登錄時間。";
      cardBody.append(review);
    }
    if (!(activity.reward_tiers || []).length) return;
    const table = document.createElement("table");
    table.className = "reward-tiers";
    table.innerHTML = "<caption>分階回饋門檻</caption><thead><tr><th>消費門檻</th><th>回饋</th><th>分期加碼</th><th>名額</th></tr></thead>";
    const body = document.createElement("tbody");
    activity.reward_tiers.forEach((tier) => {
      const row = document.createElement("tr");
      [
        `NT$${Number(tier.spend_amount_twd).toLocaleString("zh-TW")}`,
        `NT$${Number(tier.reward_amount_twd).toLocaleString("zh-TW")}`,
        `NT$${Number(tier.installment_reward_amount_twd).toLocaleString("zh-TW")}`,
        `${Number(tier.quota).toLocaleString("zh-TW")} 名`
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      body.append(row);
    });
    table.append(body);
    cardBody.append(table);
  }

  function renderActivity(activity) {
    const node = el.activityTemplate.content.cloneNode(true);
    const card = node.querySelector(".activity-card");
    card.dataset.bank = activity.bank_id;
    node.querySelector(".bank-badge strong").textContent = activity.bank_name;
    const badgeBox = node.querySelector(".status-badges");
    statusBadges(activity).forEach(([text, className]) => {
      const badge = document.createElement("span");
      badge.className = `status-badge ${className}`;
      badge.textContent = text;
      badgeBox.append(badge);
    });
    node.querySelector(".merchant-name").textContent = activity.merchant;
    node.querySelector(".card-body h3").textContent = activity.title;
    node.querySelector(".summary").textContent = activity.summary;
    node.querySelector(".activity-period").textContent = formatPeriod(activity);
    node.querySelector(".reward-value").textContent = rewardLabel(activity);

    const registrationBox = node.querySelector(".registration-box");
    if (!activity.registration_required) {
      registrationBox.hidden = true;
    } else {
      const registrationLink = node.querySelector(".registration-link");
      registrationLink.href = activity.registration_url || activity.source_url;
      registrationLink.textContent = registrationLinkText(activity);
      appendPortalHint(registrationBox, activity);
      appendTimingContracts(registrationBox, activity);
      const windowsBox = node.querySelector(".registration-windows");
      const windows = relevantWindows(activity);
      windows.forEach((window) => {
        const item = document.createElement("div");
        item.className = "registration-window";
        const text = document.createElement("p");
        const start = parseDate(window.start);
        const end = parseDate(window.end);
        const sameDay = end && taipeiDateKey(start) === taipeiDateKey(end);
        const timing = !end
          ? "起（截止時間未確認）"
          : (sameDay ? windowStatus(window) : `至 ${shortDateFmt.format(end)} ${timeFmt.format(end)}`);
        text.innerHTML = `<strong>${shortDateFmt.format(start)} ${timeFmt.format(start)}</strong><span>${timing}</span>`;
        const buttons = document.createElement("div");
        buttons.className = "registration-buttons";
        const config = calendarConfigForWindow(activity, window);
        const google = document.createElement("a");
        google.href = googleCalendarUrl(config);
        google.target = "_blank";
        google.rel = "noopener noreferrer";
        google.textContent = "G";
        google.title = "加入 Google Calendar";
        google.setAttribute("aria-label", `將「${activity.title}」登錄提醒加入 Google Calendar`);
        const ics = document.createElement("button");
        ics.type = "button";
        ics.textContent = "10";
        ics.title = "下載含 10 分鐘提醒的 .ics";
        ics.setAttribute("aria-label", `下載「${activity.title}」含 10 分鐘提醒的 ICS`);
        setIcsButton(ics, config);
        buttons.append(google, ics);
        item.append(text, buttons);
        windowsBox.append(item);
      });
      if (!windows.length) {
        const review = node.querySelector(".registration-review");
        review.textContent = activity.review_message
          || "官方註明需登錄，但尚未取得可確認的登錄時點；請先查看原始活動頁。";
        review.hidden = false;
      }
    }

    const cardBody = node.querySelector(".card-body");
    appendReviewAndTiers(cardBody, activity);
    appendTermsDetails(cardBody, activity);

    node.querySelector(".official-link").href = activity.source_url;
    const activityConfig = calendarConfigForActivity(activity);
    node.querySelector(".activity-google").href = googleCalendarUrl(activityConfig);
    setIcsButton(node.querySelector(".activity-ics"), activityConfig);
    return node;
  }

  function activityHasRegistrationOn(activity, dateKey) {
    return activity.registration_windows.some((item) => item.start.startsWith(dateKey));
  }

  function daysUntil(value) {
    const today = parseDate(taipeiDateKey());
    const date = parseDate(value, true);
    return date ? Math.floor((date - today) / DAY_MS) : Number.POSITIVE_INFINITY;
  }

  function matchesQuickFilter(activity) {
    const today = taipeiDateKey();
    const tomorrow = addDays(today, 1);
    switch (state.filter) {
      case "priority": return activity.featured && activity.lifecycle !== "ended";
      case "today": return activityHasRegistrationOn(activity, today);
      case "tomorrow": return activityHasRegistrationOn(activity, tomorrow);
      case "review": return activity.needs_review;
      case "registration": return activity.registration_required && activity.lifecycle !== "ended";
      case "high-return": return activity.high_return && activity.lifecycle !== "ended";
      case "upcoming": return activity.lifecycle === "upcoming";
      case "ending": return activity.lifecycle === "active" && daysUntil(activity.end_date) <= 7;
      case "all": return true;
      default: return true;
    }
  }

  function registrationSortValue(activity) {
    const now = Date.now();
    const upcoming = activity.registration_windows
      .map((item) => parseDate(item.start).getTime())
      .filter((value) => value >= now)
      .sort((a, b) => a - b);
    return upcoming[0] ?? Number.POSITIVE_INFINITY;
  }

  function sortActivities(values) {
    const sorted = [...values];
    sorted.sort((a, b) => {
      if (state.sort === "registration") return registrationSortValue(a) - registrationSortValue(b);
      if (state.sort === "ending") return (parseDate(a.end_date, true)?.getTime() ?? Infinity) - (parseDate(b.end_date, true)?.getTime() ?? Infinity);
      if (state.sort === "reward") {
        const aValue = Math.max(a.max_reward_percent || 0, (a.max_reward_amount_twd || 0) / 100);
        const bValue = Math.max(b.max_reward_percent || 0, (b.max_reward_amount_twd || 0) / 100);
        return bValue - aValue;
      }
      const aScore = (a.registration_required ? 4 : 0) + (a.high_return ? 2 : 0) + (a.lifecycle === "upcoming" ? 1 : 0);
      const bScore = (b.registration_required ? 4 : 0) + (b.high_return ? 2 : 0) + (b.lifecycle === "upcoming" ? 1 : 0);
      return bScore - aScore || registrationSortValue(a) - registrationSortValue(b);
    });
    return sorted;
  }

  function renderActivities() {
    const query = state.query.toLocaleLowerCase("zh-Hant");
    const values = sortActivities(state.activities.filter((activity) => {
      if (!matchesQuickFilter(activity)) return false;
      if (state.bank && activity.bank_id !== state.bank) return false;
      if (state.category && !activity.categories.includes(state.category)) return false;
      if (query) {
        const haystack = [activity.bank_name, activity.title, activity.merchant, activity.summary, ...activity.tags]
          .join(" ").toLocaleLowerCase("zh-Hant");
        if (!haystack.includes(query)) return false;
      }
      return true;
    }));
    const page = catalogState.paginate(values, state.filter, state.page);
    if (state.page !== page.page) {
      state.page = page.page;
      syncUrl("replace");
    }
    el.activityList.replaceChildren();
    page.items.forEach((activity) => el.activityList.append(renderActivity(activity)));
    el.activityList.setAttribute("aria-busy", "false");
    el.resultCount.textContent = page.paginated
      ? `${values.length} 筆活動 · 第 ${page.page}/${page.pageCount} 頁`
      : `${values.length} 筆活動`;
    el.pagination.hidden = !page.paginated;
    el.pagePrevious.disabled = page.page <= 1;
    el.pageNext.disabled = page.page >= page.pageCount;
    el.pageStatus.textContent = `第 ${page.page} 頁，共 ${page.pageCount} 頁`;
    el.emptyState.hidden = values.length > 0;
  }

  function syncUrl(method = "push") {
    const url = `${window.location.pathname}${catalogState.search(state)}${window.location.hash}`;
    window.history[method === "replace" ? "replaceState" : "pushState"]({}, "", url);
  }

  function restoreStateFromUrl() {
    Object.assign(state, catalogState.read(window.location.search));
  }

  function applyStateToControls() {
    el.searchInput.value = state.query;
    el.bankSelect.value = state.bank;
    el.categorySelect.value = state.category;
    el.sortSelect.value = state.sort;
    if (el.bankSelect.value !== state.bank) state.bank = "";
    if (el.categorySelect.value !== state.category) state.category = "";
    if (el.sortSelect.value !== state.sort) state.sort = "recommended";
    el.quickFilters.querySelectorAll("button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.filter === state.filter));
    });
  }

  function stateNeedsFullCatalog() {
    return !INDEX_FILTERS.has(state.filter);
  }

  async function loadFullCatalog() {
    if (state.catalogLoaded) return;
    if (state.catalogPromise) return state.catalogPromise;
    const files = Object.values(state.data?.catalog?.bank_files || {});
    if (!files.length) {
      state.catalogLoaded = true;
      return;
    }
    state.catalogPromise = Promise.all(files.map(async (reference) => {
      const response = await fetch(resolveDataUrl(reference), { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${reference}`);
      return response.json();
    })).then((banks) => {
      const activities = banks.flatMap((bank) => bank.activities || []);
      state.activities = activities.map((activity) => (
        timeState.deriveActivity(activity, state.data.thresholds)
      ));
      state.catalogLoaded = true;
      state.catalogPromise = null;
      updateDerivedSummary();
    }).catch((error) => {
      state.catalogPromise = null;
      throw error;
    });
    return state.catalogPromise;
  }

  async function ensureCatalogForState() {
    if (!stateNeedsFullCatalog()) return;
    el.activityList.setAttribute("aria-busy", "true");
    el.resultCount.textContent = "正在載入完整活動目錄…";
    await loadFullCatalog();
  }

  async function commitCatalogState({ scroll = false } = {}) {
    syncUrl("push");
    try {
      await ensureCatalogForState();
      renderActivities();
    } catch (error) {
      el.activityList.setAttribute("aria-busy", "false");
      el.resultCount.textContent = "完整活動目錄載入失敗";
      console.error(error);
    }
    if (scroll) document.querySelector("#catalog-title").scrollIntoView({ block: "start" });
  }

  function populateFilters() {
    const banks = (state.data.sources || []).map((item) => [item.id, item.bank_name]);
    banks.forEach(([id, name]) => el.bankSelect.add(new Option(name, id)));
    const categories = state.data.catalog?.categories
      || [...new Set(state.activities.flatMap((item) => item.categories))].sort();
    categories.forEach((name) => el.categorySelect.add(new Option(name, name)));
  }

  function renderHealth() {
    const health = state.data.source_health;
    const allComplete = health.every((item) => item.status === "complete");
    const anyFailed = health.some((item) => item.status === "failed");
    el.sourceDot.className = `source-dot ${allComplete ? "is-ok" : anyFailed ? "is-error" : ""}`;
    el.updateLabel.textContent = `${allComplete ? "官方來源正常" : "部分來源需留意"} · ${shortDateFmt.format(parseDate(state.data.generated_at))}`;
    el.sourceHealthList.replaceChildren();
    health.forEach((item) => {
      const li = document.createElement("li");
      const dot = document.createElement("span");
      dot.className = `health-indicator is-${item.status}`;
      const name = document.createElement("strong");
      name.textContent = item.bank_name;
      const status = document.createElement("span");
      const sourceStatus = item.status === "complete" ? "來源正常" : item.status === "partial" ? "部分可讀" : "讀取失敗";
      const registrationCoverage = item.registration_required_count
        ? ` · 登錄時點 ${item.registration_time_confirmed_count}/${item.registration_required_count}（${item.registration_time_coverage_percent}%）`
        : " · 無需登錄活動";
      status.textContent = `${sourceStatus} · ${item.activity_count} 筆${registrationCoverage}`;
      li.append(dot, name, status);
      el.sourceHealthList.append(li);
    });
    if (state.data.alerts.length) {
      el.alertsPanel.hidden = false;
      el.alertsPanel.textContent = state.data.alerts.map((item) => `${item.bank_name}：${item.message}`).join(" ");
    }
  }

  function setQuickFilter(filter, { push = true } = {}) {
    state.filter = filter;
    state.page = 1;
    el.quickFilters.querySelectorAll("button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.filter === filter));
    });
    if (push) commitCatalogState();
    else renderActivities();
  }

  function clearAllFilters() {
    state.query = "";
    state.bank = "";
    state.category = "";
    state.sort = "recommended";
    state.page = 1;
    el.searchInput.value = "";
    el.bankSelect.value = "";
    el.categorySelect.value = "";
    el.sortSelect.value = "recommended";
    state.filter = "all";
    applyStateToControls();
    commitCatalogState();
  }

  function bindEvents() {
    let searchTimer = 0;
    el.quickFilters.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-filter]");
      if (button) setQuickFilter(button.dataset.filter);
    });
    el.searchInput.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.query = el.searchInput.value.trim();
        state.page = 1;
        commitCatalogState();
      }, 150);
    });
    el.bankSelect.addEventListener("change", () => {
      state.bank = el.bankSelect.value;
      state.page = 1;
      commitCatalogState();
    });
    el.categorySelect.addEventListener("change", () => {
      state.category = el.categorySelect.value;
      state.page = 1;
      commitCatalogState();
    });
    el.sortSelect.addEventListener("change", () => {
      state.sort = el.sortSelect.value;
      state.page = 1;
      commitCatalogState();
    });
    el.pagePrevious.addEventListener("click", () => {
      state.page -= 1;
      commitCatalogState({ scroll: true });
    });
    el.pageNext.addEventListener("click", () => {
      state.page += 1;
      commitCatalogState({ scroll: true });
    });
    el.clearFilters.forEach((button) => button.addEventListener("click", clearAllFilters));
    document.querySelectorAll("[data-jump-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        setQuickFilter(button.dataset.jumpFilter);
        document.querySelector("#catalog-title").scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshDateSensitiveViews();
    });
    window.addEventListener("popstate", async () => {
      restoreStateFromUrl();
      applyStateToControls();
      try {
        await ensureCatalogForState();
        renderActivities();
      } catch (error) {
        console.error(error);
      }
    });
  }

  async function init() {
    restoreStateFromUrl();
    bindEvents();
    try {
      const response = await fetch(DATA_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      state.data = await response.json();
      state.activities = state.data.activities.map((activity) => (
        timeState.deriveActivity(activity, state.data.thresholds)
      ));
      updateDerivedSummary();
      el.thresholdNote.textContent = `高回饋：回饋率 ≥ ${state.data.thresholds.percent_at_least}% 或單筆／每期最高回饋 ≥ NT$${Number(state.data.thresholds.amount_twd_at_least).toLocaleString("zh-TW")}`;
      populateFilters();
      applyStateToControls();
      renderAgenda();
      renderHealth();
      await ensureCatalogForState();
      renderActivities();
      syncUrl("replace");
      window.setInterval(refreshDateSensitiveViews, 60000);
    } catch (error) {
      el.sourceDot.classList.add("is-error");
      el.updateLabel.textContent = "資料載入失敗";
      el.activityList.replaceChildren();
      const message = document.createElement("p");
      message.className = "empty-state";
      message.textContent = "目前無法載入活動資料，請稍後重新整理。";
      el.activityList.append(message);
      console.error(error);
    }
  }

  init();
})();
