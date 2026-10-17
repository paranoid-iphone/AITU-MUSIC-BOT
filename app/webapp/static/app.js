"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const initData = (tg && tg.initData) || "";
const REFRESH_MS = 30000;
const ROUTES = ["schedule", "me"];
const TELEGRAM_LANGUAGES = { ru: "ru", en: "en", kk: "kz", kz: "kz" };

const state = { me: null, strings: {}, renderToken: 0, refreshTimer: null };

if (tg) {
  document.documentElement.classList.add("tg");
  tg.ready();
  tg.expand();
}

/* ---------- helpers ---------- */

function t(key, fallback) {
  return state.strings[key] || fallback || key;
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name === "className") node.className = value;
    else if (name === "text") node.textContent = value;
    else node.setAttribute(name, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
  }
}

async function api(path) {
  const response = await fetch(`/webapp/api${path}`, {
    cache: "no-store",
    headers: { "X-Telegram-Init-Data": initData },
  });
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.json()).detail; } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, typeof detail === "string" ? detail : "");
  }
  return response.json();
}

function minutesToTime(total) {
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function timeToMinutes(value) {
  const [hours, minutes] = value.slice(0, 5).split(":").map(Number);
  return hours * 60 + minutes;
}

function shortDate(iso) {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
}

function telegramLanguage() {
  const code = tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code;
  return TELEGRAM_LANGUAGES[(code || "").slice(0, 2).toLowerCase()] || "ru";
}

/* ---------- bootstrap ---------- */

async function loadMe() {
  if (!initData) return null; // opened outside Telegram
  try {
    return await api("/me");
  } catch (_) {
    return null;
  }
}

async function loadStrings(language) {
  try {
    state.strings = await api(`/locales/${language}`);
  } catch (_) {
    state.strings = {};
  }
  document.documentElement.lang = language === "kz" ? "kk" : language;
}

function currentRoute() {
  const route = (location.hash.replace(/^#\/?/, "") || "schedule").split("/")[0];
  return ROUTES.includes(route) ? route : "schedule";
}

function renderChrome(route) {
  document.getElementById("title").textContent = t("webapp.title", "AITU Music Club");
  const nav = document.getElementById("nav");
  nav.replaceChildren(
    ...ROUTES.map((name) =>
      el("a", { href: `#/${name}`, className: name === route ? "active" : "", text: t(`webapp.nav.${name}`) })
    )
  );
  if (tg && tg.BackButton) {
    if (route === "schedule") tg.BackButton.hide();
    else tg.BackButton.show();
  }
}

/* ---------- screens ---------- */

function notice(text, isError = false) {
  return el("div", { className: `notice${isError ? " error" : ""}`, text });
}

async function renderSchedule(screen, token) {
  const data = await api("/schedule");
  if (token !== state.renderToken) return;

  const byStart = new Map();
  const covered = new Set();
  for (const item of data.bookings) {
    const start = item.start_time.slice(0, 5);
    byStart.set(`${item.booking_date}|${start}`, item);
    for (let minute = timeToMinutes(start) + data.slot_minutes; minute < timeToMinutes(item.end_time); minute += data.slot_minutes) {
      covered.add(`${item.booking_date}|${minutesToTime(minute)}`);
    }
  }
  const closedByDate = new Map(data.day_closures.map((item) => [item.date, item.occupied_by || t("webapp.day_busy_default")]));

  const head = el("tr", {}, [
    el("th", { text: t("webapp.time") }),
    ...data.days.map((day) =>
      el("th", { className: day.date === data.today ? "today" : "" }, [
        t(`webapp.weekday.${day.weekday}`),
        el("br"),
        shortDate(day.date),
      ])
    ),
  ]);
  const rows = [head];
  for (let minute = data.window_start_minutes; minute < data.window_end_minutes; minute += data.slot_minutes) {
    const time = minutesToTime(minute);
    const row = el("tr", {}, [el("td", { className: "time", text: time })]);
    for (const day of data.days) {
      const key = `${day.date}|${time}`;
      if (covered.has(key)) continue;
      const cell = el("td");
      const item = byStart.get(key);
      if (item) {
        cell.rowSpan = Math.max(1, Math.ceil((timeToMinutes(item.end_time) - timeToMinutes(item.start_time)) / data.slot_minutes));
        cell.append(bookingBlock(item));
      } else if (closedByDate.has(day.date)) {
        cell.append(el("div", { className: "closed" }, [
          el("div", { className: "closed-title", text: t("webapp.day_busy") }),
          el("div", { className: "closed-note", text: closedByDate.get(day.date) }),
        ]));
      } else {
        cell.append(el("span", { className: "empty", text: t("webapp.free") }));
      }
      row.append(cell);
    }
    rows.push(row);
  }

  screen.replaceChildren(el("div", { className: "grid-wrap" }, [el("table", { className: "schedule" }, rows)]));
  document.getElementById("meta").textContent =
    `${t("webapp.schedule.subtitle")} · ${t("webapp.updated")} ${data.now.slice(11, 16)}`;
}

function bookingBlock(item) {
  const group = item.group_title ? ` · ${item.group_title}` : "";
  const responsible = item.responsible_name
    ? `${t("webapp.responsible_short")}: ${item.responsible_name}${item.responsible_username ? " @" + item.responsible_username : ""}`
    : "";
  const purpose = t(`webapp.purpose.${item.purpose}`, item.purpose);
  return el("div", { className: `event${item.purpose === "staff" ? " staff" : ""}` }, [
    el("div", { className: "song", text: item.song_title || t("webapp.song_missing") }),
    el("div", { className: "band", text: `${item.band_name}${group}` }),
    responsible ? el("div", { className: "resp", text: responsible }) : null,
    el("div", { className: "purpose", text: `${item.start_time.slice(0, 5)}-${item.end_time.slice(0, 5)} · ${purpose}` }),
  ]);
}

function renderProfile(screen) {
  document.getElementById("meta").textContent = "";
  const me = state.me;
  if (!me) {
    screen.replaceChildren(notice(t("webapp.open_in_telegram"), true));
    return;
  }
  const cards = [];
  const name = [me.first_name, me.last_name].filter(Boolean).join(" ");
  const rows = [
    [t("webapp.profile.name"), name || "—"],
    [t("webapp.profile.username"), me.telegram_username ? `@${me.telegram_username}` : "—"],
  ];
  if (me.registered) {
    rows.push([t("webapp.profile.study_group"), me.study_group || "—"]);
    rows.push([t("webapp.profile.club_group"), me.group ? me.group.title : "—"]);
    rows.push([t("webapp.profile.status"), t(`webapp.status.${me.status}`, me.status)]);
  }
  cards.push(el("div", { className: "card" }, [
    el("h2", { text: t("webapp.profile.title") }),
    ...rows.map(([label, value]) => el("div", { className: "row" }, [el("span", { text: label }), el("span", { text: value })])),
  ]));
  if (!me.registered) {
    cards.push(notice(t("webapp.profile.not_registered")));
  } else {
    const badges = [];
    if (me.is_admin) badges.push(t("webapp.role.admin"));
    if (me.is_dsvr) badges.push(t("webapp.role.dsvr"));
    for (const group of me.administered_groups) badges.push(`${t("webapp.role.group_admin")}: ${group.title}`);
    if (badges.length) {
      cards.push(el("div", { className: "card" }, [
        el("h2", { text: t("webapp.profile.roles") }),
        el("div", { className: "badges" }, badges.map((text) => el("span", { className: "badge", text }))),
      ]));
    }
  }
  screen.replaceChildren(...cards);
}

/* ---------- router ---------- */

async function render() {
  const token = ++state.renderToken;
  const route = currentRoute();
  const screen = document.getElementById("screen");
  renderChrome(route);
  clearTimeout(state.refreshTimer);
  try {
    if (route === "me") renderProfile(screen);
    else await renderSchedule(screen, token);
  } catch (error) {
    if (token === state.renderToken) {
      screen.replaceChildren(notice(t("webapp.error.load"), true));
    }
  }
  if (route === "schedule" && token === state.renderToken) {
    // While the app is in the background the timer idles; the visibilitychange handler refreshes on return.
    state.refreshTimer = setTimeout(() => { if (!document.hidden) render(); }, REFRESH_MS);
  }
}

async function main() {
  state.me = await loadMe();
  await loadStrings(state.me ? state.me.language : telegramLanguage());
  if (tg && tg.BackButton) tg.BackButton.onClick(() => { location.hash = "#/schedule"; });
  window.addEventListener("hashchange", render);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && currentRoute() === "schedule") render();
  });
  render();
}

main();
