(function attachCalendarState(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CardPromotionCalendar = api;
}(typeof globalThis === "object" ? globalThis : this, () => {
  "use strict";

  function escapeIcs(value) {
    return String(value ?? "")
      .replaceAll("\\", "\\\\")
      .replaceAll("\n", "\\n")
      .replaceAll(",", "\\,")
      .replaceAll(";", "\\;");
  }

  function foldIcsLine(line) {
    const physical = [];
    let current = "";
    for (const character of String(line)) {
      const candidate = current + character;
      if (current && new TextEncoder().encode(candidate).length > 75) {
        physical.push(current);
        current = ` ${character}`;
      } else {
        current = candidate;
      }
    }
    physical.push(current);
    return physical.join("\r\n");
  }

  function serializeIcs(lines) {
    return lines.map(foldIcsLine).join("\r\n");
  }

  return { escapeIcs, foldIcsLine, serializeIcs };
}));
