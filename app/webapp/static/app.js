"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const initData = (tg && tg.initData) || "";
const REFRESH_MS = 30000;
const TOAST_MS = 3000;
const ROUTES = ["schedule", "mine", "bands", "admin", "me"];
const TELEGRAM_LANGUAGES = { ru: "ru", en: "en", kk: "kz", kz: "kz" };
const BOOKABLE_PURPOSES = ["self", "concert"];
const PERSONAL = "personal";
const JOIN_PARAM = /^join_([A-Za-z0-9]{1,30})$/;

const state = {
  me: null,
  strings: {},
  renderToken: 0,
  refreshTimer: null,
  data: null, // last /schedule payload
  free: new Map(), // date -> Set of "HH:MM" cells that can be booked
  selection: null, // { date, from, to } in minutes; `to` is exclusive
  closeSheet: null, // set while a bottom sheet is open
  adminTab: "requests",
  adminQuery: "",
  adminStatus: "",
  adminSearchToken: 0,
};

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
    this.detail = detail || "";
  }
}

async function api(path, options = {}) {
  const headers = { "X-Telegram-Init-Data": initData };
  const init = { cache: "no-store", method: options.method || "GET", headers };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(`/webapp/api${path}`, init);
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

function weekdayLabel(iso) {
  const [year, month, day] = iso.split("-").map(Number);
  return t(`webapp.weekday.${(new Date(year, month - 1, day).getDay() + 6) % 7}`);
}

function formatMinutes(total) {
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  const parts = [];
  if (hours) parts.push(`${hours} ${t("webapp.unit.h")}`);
  if (minutes || !hours) parts.push(`${minutes} ${t("webapp.unit.min")}`);
  return parts.join(" ");
}

function telegramLanguage() {
  const code = tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code;
  return TELEGRAM_LANGUAGES[(code || "").slice(0, 2).toLowerCase()] || "ru";
}

function canBook() {
  return Boolean(state.me && state.me.registered && state.me.status === "approved");
}

function haptic(kind) {
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(kind);
}

function openTelegramUser(username) {
  const url = `https://t.me/${encodeURIComponent(username)}`;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, "_blank", "noopener");
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

function syncBackButton() {
  if (!tg || !tg.BackButton) return;
  if (state.closeSheet || currentRoute() !== "schedule") tg.BackButton.show();
  else tg.BackButton.hide();
}

function renderChrome(route) {
  document.body.dataset.route = route; // lets the desktop CSS give the schedule a wider column than the other screens
  document.getElementById("title").textContent = t("webapp.title", "AITU Music Club");
  const nav = document.getElementById("nav");
  nav.replaceChildren(
    ...ROUTES.filter((name) => name !== "admin" || canAdmin()).map((name) =>
      el("a", { href: `#/${name}`, className: name === route ? "active" : "", text: t(`webapp.nav.${name}`) })
    )
  );
  syncBackButton();
}

/* ---------- overlays ---------- */

function toast(text, isError = false) {
  const node = document.getElementById("toast");
  node.textContent = text;
  node.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = "toast"; }, TOAST_MS);
}

function openSheet(children) {
  closeSheet();
  const overlay = el("div", { className: "overlay" });
  const sheet = el("div", { className: "sheet", role: "dialog" }, children);
  overlay.append(sheet);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) closeSheet(); });
  document.body.append(overlay);
  state.closeSheet = () => {
    overlay.remove();
    state.closeSheet = null;
    syncBackButton();
  };
  syncBackButton();
}

function closeSheet() {
  if (state.closeSheet) state.closeSheet();
}

/* ---------- schedule ---------- */

function cellsAreFree(date, from, to) {
  const free = state.free.get(date);
  if (!free) return false;
  for (let minute = from; minute < to; minute += state.data.slot_minutes) {
    if (!free.has(minutesToTime(minute))) return false;
  }
  return true;
}

function pickCell(date, minute) {
  const step = state.data.slot_minutes;
  const selection = state.selection;
  const tappedSelected = Boolean(selection) && selection.date === date && minute >= selection.from && minute < selection.to;
  if (!selection || selection.date !== date) {
    state.selection = { date, from: minute, to: minute + step };
  } else if (tappedSelected) {
    // tapping a selected cell switches it off; the range must stay in one piece, so a middle cell cuts it short
    if (minute === selection.from) state.selection = selection.to - step > minute ? { ...selection, from: minute + step } : null;
    else state.selection = { ...selection, to: minute };
  } else {
    const from = Math.min(selection.from, minute);
    const to = Math.max(selection.to, minute + step);
    // a range may not jump over a busy cell: start over from the tapped one instead
    state.selection = cellsAreFree(date, from, to) ? { date, from, to } : { date, from: minute, to: minute + step };
  }
  applySelection();
}

function applySelection() {
  const selection = state.selection;
  for (const cell of document.querySelectorAll("td.free")) {
    const minute = timeToMinutes(cell.dataset.time);
    const selected = Boolean(selection) && cell.dataset.date === selection.date && minute >= selection.from && minute < selection.to;
    cell.classList.toggle("selected", selected);
  }
  renderSelectionBar();
}

function renderSelectionBar() {
  const bar = document.getElementById("selbar");
  const selection = state.selection;
  document.body.classList.toggle("has-selbar", Boolean(selection));
  if (!selection) {
    bar.className = "selbar"; // keep the old content so the bar can slide out with it
    return;
  }
  bar.className = "selbar show";
  bar.replaceChildren(
    el("div", { className: "selbar-text" }, [
      el("strong", { text: `${weekdayLabel(selection.date)} ${shortDate(selection.date)}` }),
      ` ${minutesToTime(selection.from)}–${minutesToTime(selection.to)} · ${formatMinutes(selection.to - selection.from)}`,
    ]),
    el("button", { className: "btn secondary", type: "button", text: t("webapp.select.clear") }),
    el("button", { className: "btn", type: "button", text: t("webapp.select.book") })
  );
  const [clear, book] = bar.querySelectorAll("button");
  clear.addEventListener("click", () => { state.selection = null; applySelection(); });
  book.addEventListener("click", openBookingForm);
}

async function renderSchedule(screen, token) {
  const data = await api("/schedule");
  if (token !== state.renderToken) return;
  state.data = data;
  state.free = new Map(Object.entries(data.free_slots).map(([date, times]) => [date, new Set(times)]));
  const selection = state.selection;
  if (selection && !cellsAreFree(selection.date, selection.from, selection.to)) state.selection = null;

  const byStart = new Map();
  const covered = new Set();
  for (const item of data.bookings) {
    const start = item.start_time.slice(0, 5);
    byStart.set(`${item.booking_date}|${start}`, item);
    for (let minute = timeToMinutes(start) + data.slot_minutes; minute < timeToMinutes(item.end_time); minute += data.slot_minutes) {
      covered.add(`${item.booking_date}|${minutesToTime(minute)}`);
    }
  }
  const closedByDate = new Map(data.day_closures.map((item) => [item.date, item.occupied_by || ""]));
  // A closed day is one tall block per gap between bookings, so a booking made before the closure stays visible.
  const closedStart = new Map();
  for (const day of data.days) {
    if (!closedByDate.has(day.date)) continue;
    let runStart = null;
    let runLength = 0;
    const endRun = () => {
      if (runStart !== null) closedStart.set(`${day.date}|${runStart}`, runLength);
      runStart = null;
      runLength = 0;
    };
    for (let minute = data.window_start_minutes; minute < data.window_end_minutes; minute += data.slot_minutes) {
      const time = minutesToTime(minute);
      const key = `${day.date}|${time}`;
      if (byStart.has(key) || covered.has(key)) {
        endRun();
        continue;
      }
      if (runStart === null) runStart = time;
      else covered.add(key);
      runLength += 1;
    }
    endRun();
  }

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
  const bookable = canBook();
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
        const block = bookingBlock(item);
        if (cell.rowSpan === 1) block.classList.add("short");
        cell.append(block);
      } else if (closedStart.has(key)) {
        cell.rowSpan = closedStart.get(key);
        cell.append(el("div", { className: "closed" }, [
          el("div", { className: "closed-label" }, [
            el("div", { className: "closed-title", text: t("webapp.day_busy") }),
            closedByDate.get(day.date) ? el("div", { className: "closed-note", text: closedByDate.get(day.date) }) : null,
          ]),
        ]));
      } else {
        const isFree = state.free.has(day.date) && state.free.get(day.date).has(time);
        if (isFree && bookable) {
          cell.className = "free";
          cell.dataset.date = day.date;
          cell.dataset.time = time;
        }
        cell.append(el("span", { className: "empty", text: isFree ? t("webapp.free") : "" }));
      }
      row.append(cell);
    }
    rows.push(row);
  }

  const table = el("table", { className: "schedule" }, rows);
  table.addEventListener("click", (event) => {
    const cell = event.target.closest("td.free");
    if (cell) pickCell(cell.dataset.date, timeToMinutes(cell.dataset.time));
  });
  const previous = screen.querySelector(".grid-wrap");
  const wrap = el("div", { className: "grid-wrap" }, [table]);
  const hint = el("div", { className: "notice hint", text: t(bookable ? "webapp.select.hint" : "webapp.select.hint_readonly") });
  screen.replaceChildren(hint, wrap);
  if (previous) {
    wrap.scrollLeft = previous.scrollLeft;
    wrap.scrollTop = previous.scrollTop;
  }
  applySelection();
  document.getElementById("meta").textContent =
    `${t("webapp.schedule.subtitle")} · ${t("webapp.updated")} ${data.now.slice(11, 16)}`;
}

function bookingBlock(item) {
  const group = item.group_title ? ` · ${item.group_title}` : "";
  const responsible = item.responsible_name
    ? `${t("webapp.responsible_short")}: ${item.responsible_name}${item.responsible_username ? " @" + item.responsible_username : ""}`
    : "";
  const purpose = t(`webapp.purpose.${item.purpose}`, item.purpose);
  const block = el("div", { className: `event${item.purpose === "staff" ? " staff" : ""}`, role: "button", tabindex: "0" }, [
    el("div", { className: "song", text: item.song_title || t("webapp.song_missing") }),
    el("div", { className: "band", text: `${item.band_name}${group}` }),
    responsible ? el("div", { className: "resp", text: responsible }) : null,
    el("div", { className: "purpose", text: `${item.start_time.slice(0, 5)}-${item.end_time.slice(0, 5)} · ${purpose}` }),
  ]);
  block.addEventListener("click", () => openBookingCard(item));
  return block;
}

function detailRow(label, value) {
  return value ? el("div", { className: "row" }, [el("span", { text: label }), el("span", { text: value })]) : null;
}

function openBookingCard(item) {
  const responsible = item.responsible_name
    ? item.responsible_name + (item.responsible_username ? ` (@${item.responsible_username})` : "")
    : "";
  const actions = [];
  if (item.responsible_username) {
    const write = el("button", { className: "btn", type: "button", text: t("webapp.card.write") });
    write.addEventListener("click", () => openTelegramUser(item.responsible_username));
    actions.push(write);
  }
  const close = el("button", { className: "btn secondary", type: "button", text: t("webapp.card.close") });
  close.addEventListener("click", closeSheet);
  actions.push(close);
  openSheet([
    el("h2", { text: item.song_title || t("webapp.song_missing") }),
    detailRow(t("webapp.card.when"), `${weekdayLabel(item.booking_date)} ${shortDate(item.booking_date)} · ${item.start_time.slice(0, 5)}–${item.end_time.slice(0, 5)}`),
    detailRow(t("webapp.card.band"), item.band_name),
    detailRow(t("webapp.card.group"), item.group_title),
    detailRow(t("webapp.card.booked_by"), item.created_by),
    detailRow(t("webapp.card.responsible"), responsible),
    detailRow(t("webapp.card.purpose"), t(`webapp.purpose.${item.purpose}`, item.purpose)),
    el("div", { className: "actions" }, actions),
  ]);
}

/* ---------- booking form ---------- */

async function openBookingForm() {
  const selection = state.selection;
  if (!selection) return;
  let options;
  try {
    options = await api("/my-bands");
  } catch (error) {
    toast(error.status === 401 ? t("webapp.error.session") : t("webapp.error.load"), true);
    return;
  }

  const choices = options.bands.map((band) => el("option", { value: String(band.id), text: band.name }));
  if (options.personal_enabled) choices.push(el("option", { value: PERSONAL, text: t("webapp.form.personal") }));
  const summary = el("div", { className: "summary", text: `${weekdayLabel(selection.date)} ${shortDate(selection.date)} · ${minutesToTime(selection.from)}–${minutesToTime(selection.to)} · ${formatMinutes(selection.to - selection.from)}` });
  const close = el("button", { className: "btn secondary", type: "button", text: t("webapp.card.close") });
  close.addEventListener("click", closeSheet);
  if (!choices.length) {
    openSheet([el("h2", { text: t("webapp.form.title") }), summary, el("div", { className: "notice", text: t("webapp.form.no_bands") }), el("div", { className: "actions" }, [close])]);
    return;
  }

  const who = el("select", { id: "who" }, choices);
  const purpose = el("select", { id: "purpose" }, BOOKABLE_PURPOSES.map((name) => el("option", { value: name, text: t(`webapp.purpose.${name}`) })));
  const song = el("input", { id: "song", type: "text", maxlength: "150", autocomplete: "off" });
  const limits = el("div", { className: "limits" });
  const error = el("div", { className: "form-error" });
  const submit = el("button", { className: "btn", type: "submit", text: t("webapp.form.submit") });
  const cancel = el("button", { className: "btn secondary", type: "button", text: t("webapp.form.cancel") });
  cancel.addEventListener("click", closeSheet);
  const purposeField = el("label", {}, [t("webapp.form.purpose"), purpose]);

  async function refreshLimits() {
    const personal = who.value === PERSONAL;
    purposeField.hidden = personal;
    limits.textContent = "";
    if (personal) return;
    const bandId = who.value;
    try {
      const status = await api(`/limits?band_id=${encodeURIComponent(bandId)}&date=${selection.date}`);
      if (who.value !== bandId) return; // the choice changed while the request was running
      const parts = [`${t("webapp.form.limit_week")}: ${formatMinutes(status.remaining_minutes)}`];
      if (status.daily_limit_minutes > 0) parts.push(`${t("webapp.form.limit_day")}: ${formatMinutes(status.daily_remaining_minutes)}`);
      limits.textContent = parts.join(" · ");
    } catch (_) { /* limits are informative; the server enforces them */ }
  }
  who.addEventListener("change", refreshLimits);

  const form = el("form", { className: "form" }, [
    el("h2", { text: t("webapp.form.title") }),
    summary,
    el("label", {}, [t("webapp.form.who"), who]),
    purposeField,
    el("label", {}, [t("webapp.form.song"), song]),
    limits,
    error,
    el("div", { className: "actions" }, [submit, cancel]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const personal = who.value === PERSONAL;
    submit.disabled = true;
    error.textContent = "";
    try {
      await api("/bookings", {
        method: "POST",
        body: {
          band_id: personal ? null : Number(who.value),
          booking_date: selection.date,
          start_time: minutesToTime(selection.from),
          end_time: minutesToTime(selection.to),
          purpose: personal ? PERSONAL : purpose.value,
          song_title: song.value.trim() || null,
        },
      });
    } catch (failure) {
      submit.disabled = false;
      if (failure.status === 401) error.textContent = t("webapp.error.session");
      else error.textContent = `${t("webapp.error.save")}${failure.detail ? `: ${failure.detail}` : ""}`;
      if (failure.status === 409) render(); // somebody else may have taken the time: show the fresh grid
      haptic("error");
      return;
    }
    state.selection = null;
    closeSheet();
    haptic("success");
    toast(t("webapp.booked"));
    render();
  });
  openSheet([form]);
  refreshLimits();
}

/* ---------- my bookings ---------- */

function bookingWhen(item) {
  return `${weekdayLabel(item.booking_date)} ${shortDate(item.booking_date)} · ${item.start_time.slice(0, 5)}–${item.end_time.slice(0, 5)}`;
}

function sessionAwareMessage(failure, fallbackKey) {
  if (failure.status === 401) return t("webapp.error.session");
  return `${t(fallbackKey)}${failure.detail ? `: ${failure.detail}` : ""}`;
}

async function renderMine(screen, token) {
  document.getElementById("meta").textContent = "";
  if (!canBook()) {
    screen.replaceChildren(notice(t("webapp.select.hint_readonly")));
    return;
  }
  const { bookings } = await api("/my-bookings");
  if (token !== state.renderToken) return;
  if (!bookings.length) {
    screen.replaceChildren(notice(t("webapp.mine.empty")));
    return;
  }
  const cards = bookings.map((item) => {
    const actions = [];
    if (item.can_manage) {
      const edit = el("button", { className: "btn", type: "button", text: t("webapp.mine.edit") });
      edit.addEventListener("click", () => openEditForm(item));
      const cancel = el("button", { className: "btn secondary", type: "button", text: t("webapp.mine.cancel") });
      cancel.addEventListener("click", () => openCancelConfirm(item));
      actions.push(edit, cancel);
    }
    return el("div", { className: "card" }, [
      el("h2", { text: item.song_title || t("webapp.song_missing") }),
      detailRow(t("webapp.card.when"), bookingWhen(item)),
      detailRow(t("webapp.card.band"), item.band_name),
      detailRow(t("webapp.card.purpose"), t(`webapp.purpose.${item.purpose}`, item.purpose)),
      actions.length ? el("div", { className: "actions" }, actions) : el("div", { className: "meta", text: t("webapp.mine.read_only") }),
    ]);
  });
  screen.replaceChildren(...cards);
}

async function openEditForm(item) {
  let schedule;
  try {
    schedule = await api("/schedule");
  } catch (_) {
    toast(t("webapp.error.load"), true);
    return;
  }
  const step = schedule.slot_minutes;
  const ownFrom = timeToMinutes(item.start_time);
  const ownTo = timeToMinutes(item.end_time);
  const dates = Object.keys(schedule.free_slots).sort();
  if (!dates.includes(item.booking_date)) dates.unshift(item.booking_date);

  // The booking's own cells count as free: it may slide over the time it already holds.
  function freeMinutes(date) {
    const cells = new Set((schedule.free_slots[date] || []).map(timeToMinutes));
    if (date === item.booking_date) for (let minute = ownFrom; minute < ownTo; minute += step) cells.add(minute);
    return cells;
  }
  function endChoices(cells, from) {
    const ends = [];
    for (let minute = from; cells.has(minute); minute += step) ends.push(minute + step);
    return ends;
  }

  const day = el("select", { id: "edit-day" }, dates.map((date) => el("option", { value: date, text: `${weekdayLabel(date)} ${shortDate(date)}` })));
  const start = el("select", { id: "edit-start" });
  const end = el("select", { id: "edit-end" });
  const song = el("input", { id: "edit-song", type: "text", maxlength: "150", autocomplete: "off" });
  song.value = item.song_title || "";
  const purpose = el("select", { id: "edit-purpose" }, BOOKABLE_PURPOSES.map((name) => el("option", { value: name, text: t(`webapp.purpose.${name}`) })));
  purpose.value = BOOKABLE_PURPOSES.includes(item.purpose) ? item.purpose : BOOKABLE_PURPOSES[0];
  const purposeField = el("label", {}, [t("webapp.form.purpose"), purpose]);
  purposeField.hidden = item.band_id === null || item.band_id === undefined;
  const error = el("div", { className: "form-error" });
  const save = el("button", { className: "btn", type: "submit", text: t("webapp.edit.save") });
  const cancel = el("button", { className: "btn secondary", type: "button", text: t("webapp.form.cancel") });
  cancel.addEventListener("click", closeSheet);

  let length = ownTo - ownFrom; // the length the user wants; kept while the start and the day change
  function fillTimes(preferredStart) {
    const cells = freeMinutes(day.value);
    const starts = [...cells].sort((a, b) => a - b);
    start.replaceChildren(...starts.map((minute) => el("option", { value: String(minute), text: minutesToTime(minute) })));
    save.disabled = !starts.length;
    if (!starts.length) {
      end.replaceChildren();
      error.textContent = t("webapp.edit.no_free");
      return;
    }
    error.textContent = "";
    start.value = String(starts.includes(preferredStart) ? preferredStart : starts[0]);
    fillEnds();
  }
  function fillEnds() {
    const from = Number(start.value);
    const ends = endChoices(freeMinutes(day.value), from);
    end.replaceChildren(...ends.map((minute) => el("option", { value: String(minute), text: minutesToTime(minute) })));
    const wanted = from + length;
    end.value = String(ends.includes(wanted) ? wanted : ends[0]);
  }
  day.addEventListener("change", () => fillTimes(Number(start.value)));
  start.addEventListener("change", fillEnds);
  end.addEventListener("change", () => { length = Number(end.value) - Number(start.value); });
  day.value = item.booking_date;
  fillTimes(ownFrom);

  const form = el("form", { className: "form" }, [
    el("h2", { text: t("webapp.edit.title") }),
    el("div", { className: "summary", text: `${item.band_name} · ${bookingWhen(item)}` }),
    el("label", {}, [t("webapp.edit.date"), day]),
    el("label", {}, [t("webapp.edit.start"), start]),
    el("label", {}, [t("webapp.edit.end"), end]),
    purposeField,
    el("label", {}, [t("webapp.form.song"), song]),
    error,
    el("div", { className: "actions" }, [save, cancel]),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {};
    const newStart = minutesToTime(Number(start.value));
    const newEnd = minutesToTime(Number(end.value));
    if (day.value !== item.booking_date) body.booking_date = day.value;
    if (newStart !== item.start_time.slice(0, 5)) body.start_time = newStart;
    if (newEnd !== item.end_time.slice(0, 5)) body.end_time = newEnd;
    // moving only one edge still sends both, so the server validates the whole new range
    if (body.start_time || body.end_time) { body.start_time = newStart; body.end_time = newEnd; }
    if (!purposeField.hidden && purpose.value !== item.purpose) body.purpose = purpose.value;
    if (song.value.trim() !== (item.song_title || "")) body.song_title = song.value.trim();
    if (!Object.keys(body).length) { closeSheet(); return; }
    save.disabled = true;
    error.textContent = "";
    try {
      await api(`/bookings/${item.id}`, { method: "PATCH", body });
    } catch (failure) {
      save.disabled = false;
      error.textContent = sessionAwareMessage(failure, "webapp.edit.error");
      haptic("error");
      return;
    }
    closeSheet();
    haptic("success");
    toast(t("webapp.edit.saved"));
    render();
  });
  openSheet([form]);
}

function openCancelConfirm(item) {
  const error = el("div", { className: "form-error" });
  const confirm = el("button", { className: "btn", type: "button", text: t("webapp.cancel.confirm") });
  const keep = el("button", { className: "btn secondary", type: "button", text: t("webapp.cancel.keep") });
  keep.addEventListener("click", closeSheet);
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    error.textContent = "";
    try {
      await api(`/bookings/${item.id}`, { method: "DELETE" });
    } catch (failure) {
      confirm.disabled = false;
      error.textContent = sessionAwareMessage(failure, "webapp.cancel.error");
      haptic("error");
      return;
    }
    closeSheet();
    haptic("success");
    toast(t("webapp.cancel.done"));
    render();
  });
  openSheet([
    el("h2", { text: t("webapp.cancel.title") }),
    el("div", { className: "summary", text: `${item.band_name} · ${bookingWhen(item)}` }),
    error,
    el("div", { className: "actions" }, [confirm, keep]),
  ]);
}

/* ---------- bands ---------- */

function fill(key, values) {
  return Object.entries(values).reduce((text, [name, value]) => text.replace(`{${name}}`, value), t(key));
}

function copyText(text) {
  try {
    navigator.clipboard.writeText(text).then(() => toast(t("webapp.bands.copied")), () => toast(text));
  } catch (_) {
    toast(text);
  }
}

function sheetActions(...buttons) {
  return el("div", { className: "actions" }, buttons);
}

function textField(label, id, value = "", maxlength = 150) {
  const input = el("input", { id, type: "text", maxlength: String(maxlength), autocomplete: "off" });
  input.value = value;
  return { input, field: el("label", {}, [label, input]) };
}

// One form sheet: `run(values)` rejects with an ApiError to keep the sheet open.
function openBandForm({ title, fields, submitLabel, errorKey, doneKey, run }) {
  const error = el("div", { className: "form-error" });
  const submit = el("button", { className: "btn", type: "submit", text: submitLabel });
  const cancel = el("button", { className: "btn secondary", type: "button", text: t("webapp.form.cancel") });
  cancel.addEventListener("click", closeSheet);
  const form = el("form", { className: "form" }, [
    el("h2", { text: title }),
    ...fields.map((item) => item.field),
    error,
    sheetActions(submit, cancel),
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (fields.some((item) => item.required && !item.input.value.trim())) return;
    submit.disabled = true;
    error.textContent = "";
    try {
      await run(fields.map((item) => item.input.value.trim()));
    } catch (failure) {
      submit.disabled = false;
      error.textContent = sessionAwareMessage(failure, errorKey);
      haptic("error");
      return;
    }
    closeSheet();
    haptic("success");
    toast(t(doneKey));
    render();
  });
  openSheet([form]);
  fields[0].input.focus();
}

function openConfirm({ title, text, confirmLabel, errorKey, doneKey, run }) {
  const error = el("div", { className: "form-error" });
  const confirm = el("button", { className: "btn", type: "button", text: confirmLabel });
  const keep = el("button", { className: "btn secondary", type: "button", text: t("webapp.bands.keep") });
  keep.addEventListener("click", closeSheet);
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    error.textContent = "";
    try {
      await run();
    } catch (failure) {
      confirm.disabled = false;
      error.textContent = sessionAwareMessage(failure, errorKey);
      haptic("error");
      return;
    }
    closeSheet();
    haptic("success");
    toast(t(doneKey));
    render();
  });
  openSheet([el("h2", { text: title }), el("div", { className: "summary", text }), error, sheetActions(confirm, keep)]);
}

function openCreateBandForm() {
  const name = textField(t("webapp.bands.name"), "band-name");
  name.required = true;
  const role = textField(t("webapp.bands.role"), "band-role", "", 80);
  openBandForm({
    title: t("webapp.bands.create_title"),
    fields: [name, role],
    submitLabel: t("webapp.bands.create_submit"),
    errorKey: "webapp.bands.create_error",
    doneKey: "webapp.bands.created",
    run: ([bandName, bandRole]) => api("/bands", { method: "POST", body: { name: bandName, instrument_role: bandRole || "owner" } }),
  });
}

function openJoinForm(code = "") {
  const codeField = textField(t("webapp.bands.code_input"), "band-code", code, 30);
  codeField.required = true;
  const role = textField(t("webapp.bands.role"), "band-join-role", "", 80);
  openBandForm({
    title: t("webapp.bands.join_title"),
    fields: [codeField, role],
    submitLabel: t("webapp.bands.join_submit"),
    errorKey: "webapp.bands.join_error",
    doneKey: "webapp.bands.joined",
    run: ([inviteCode, bandRole]) => api("/bands/join", { method: "POST", body: { invite_code: inviteCode, instrument_role: bandRole || "other" } }),
  });
}

function openRenameForm(band) {
  const name = textField(t("webapp.bands.name"), "band-rename", band.name);
  name.required = true;
  openBandForm({
    title: t("webapp.bands.rename_title"),
    fields: [name],
    submitLabel: t("webapp.bands.save"),
    errorKey: "webapp.bands.rename_error",
    doneKey: "webapp.bands.renamed",
    run: ([bandName]) => api(`/bands/${band.id}`, { method: "PATCH", body: { name: bandName } }),
  });
}

function openDeleteBand(band) {
  openConfirm({
    title: t("webapp.bands.delete_title"),
    text: fill("webapp.bands.delete_confirm", { name: band.name }),
    confirmLabel: t("webapp.bands.delete_yes"),
    errorKey: "webapp.bands.member_error",
    doneKey: "webapp.bands.deleted",
    run: () => api(`/bands/${band.id}`, { method: "DELETE" }),
  });
}

function openMemberSheet(band, member) {
  const buttons = [];
  if (member.telegram_username && !member.is_me) {
    const write = el("button", { className: "btn", type: "button", text: t("webapp.bands.write") });
    write.addEventListener("click", () => openTelegramUser(member.telegram_username));
    buttons.push(write);
  }
  const children = [
    el("h2", { text: member.name }),
    detailRow(t("webapp.bands.role_member"), member.instrument_role),
    detailRow(t("webapp.profile.username"), member.telegram_username ? `@${member.telegram_username}` : ""),
  ];
  if (band.can_manage) {
    const role = textField(t("webapp.bands.role_member"), "member-role", member.instrument_role, 80);
    const save = el("button", { className: "btn", type: "button", text: t("webapp.bands.save") });
    const error = el("div", { className: "form-error" });
    save.addEventListener("click", async () => {
      const value = role.input.value.trim();
      if (!value || value === member.instrument_role) return;
      save.disabled = true;
      try {
        await api(`/bands/${band.id}/members/${member.id}`, { method: "PATCH", body: { instrument_role: value } });
      } catch (failure) {
        save.disabled = false;
        error.textContent = sessionAwareMessage(failure, "webapp.bands.member_error");
        haptic("error");
        return;
      }
      closeSheet();
      haptic("success");
      toast(t("webapp.bands.member_saved"));
      render();
    });
    children.push(el("div", { className: "form" }, [role.field, error, sheetActions(save)]));

    const confirmable = (label, text, doneKey, run) => {
      const button = el("button", { className: "btn secondary", type: "button", text: label });
      button.addEventListener("click", () => openConfirm({ title: label, text, confirmLabel: label, errorKey: "webapp.bands.member_error", doneKey, run }));
      buttons.push(button);
    };
    if (!member.is_responsible) {
      confirmable(t("webapp.bands.make_responsible"), member.name, "webapp.bands.responsible_done",
        () => api(`/bands/${band.id}/responsible`, { method: "PATCH", body: { member_id: member.id } }));
    }
    if (!member.is_owner) {
      confirmable(t("webapp.bands.make_leader"), fill("webapp.bands.leader_confirm", { name: member.name }), "webapp.bands.leader_done",
        () => api(`/bands/${band.id}/owner`, { method: "PATCH", body: { member_id: member.id } }));
      confirmable(t("webapp.bands.remove"), fill("webapp.bands.remove_confirm", { name: member.name }), "webapp.bands.removed",
        () => api(`/bands/${band.id}/members/${member.id}`, { method: "DELETE" }));
    }
  }
  const close = el("button", { className: "btn secondary", type: "button", text: t("webapp.card.close") });
  close.addEventListener("click", closeSheet);
  buttons.push(close);
  children.push(sheetActions(...buttons));
  openSheet(children);
}

function memberRow(band, member) {
  const tags = [];
  if (member.is_owner) tags.push(t("webapp.bands.leader"));
  if (member.is_responsible) tags.push(t("webapp.bands.responsible"));
  if (member.is_me) tags.push(t("webapp.bands.you"));
  const row = el("div", { className: "member", role: "button", tabindex: "0" }, [
    el("div", {}, [
      el("div", { className: "member-name", text: member.name }),
      el("div", { className: "meta", text: [member.instrument_role, member.study_group].filter(Boolean).join(" · ") }),
    ]),
    tags.length ? el("div", { className: "badges" }, tags.map((text) => el("span", { className: "badge", text }))) : null,
  ]);
  row.addEventListener("click", () => openMemberSheet(band, member));
  return row;
}

function bandCard(band) {
  const children = [el("h2", { text: band.name })];
  if (band.group_title) children.push(el("div", { className: "meta", text: band.group_title }));
  if (band.invite_code) {
    const copy = el("button", { className: "btn secondary", type: "button", text: t("webapp.bands.copy") });
    copy.addEventListener("click", () => copyText(band.invite_code));
    children.push(el("div", { className: "code" }, [
      el("span", { text: t("webapp.bands.code") }),
      el("strong", { className: "code-value", text: band.invite_code }),
      copy,
    ]));
  }
  children.push(el("div", { className: "section-title", text: t("webapp.bands.members") }));
  children.push(...band.members.map((member) => memberRow(band, member)));
  if (band.can_manage) {
    const rename = el("button", { className: "btn", type: "button", text: t("webapp.bands.rename") });
    rename.addEventListener("click", () => openRenameForm(band));
    const remove = el("button", { className: "btn secondary", type: "button", text: t("webapp.bands.delete") });
    remove.addEventListener("click", () => openDeleteBand(band));
    children.push(sheetActions(rename, remove));
  } else {
    children.push(el("div", { className: "meta", text: t("webapp.bands.read_only") }));
  }
  return el("div", { className: "card" }, children);
}

async function renderBands(screen, token) {
  document.getElementById("meta").textContent = "";
  if (!canBook()) {
    screen.replaceChildren(notice(t("webapp.select.hint_readonly")));
    return;
  }
  const { bands } = await api("/bands");
  if (token !== state.renderToken) return;
  const create = el("button", { className: "btn", type: "button", text: t("webapp.bands.create") });
  create.addEventListener("click", openCreateBandForm);
  const join = el("button", { className: "btn secondary", type: "button", text: t("webapp.bands.join") });
  join.addEventListener("click", () => openJoinForm());
  screen.replaceChildren(
    el("div", { className: "actions toolbar" }, [create, join]),
    ...(bands.length ? bands.map(bandCard) : [notice(t("webapp.bands.empty"))])
  );
}

/* ---------- admin ---------- */

const ADMIN_TABS = ["requests", "users", "roles"];
const USER_STATUSES = ["pending", "approved", "rejected", "banned", "deleted"];
const REASON_ACTIONS = ["reject", "ban", "delete"];
const SEARCH_DEBOUNCE_MS = 300;

function canAdmin() {
  return Boolean(canBook() && (state.me.is_admin || state.me.administered_groups.length));
}

function personName(person) {
  const full = [person.last_name, person.first_name].filter(Boolean).join(" ");
  return full || (person.telegram_username ? `@${person.telegram_username}` : String(person.telegram_id));
}

// Only offer what the backend will accept; it still enforces the same rules.
function personActions(person) {
  if (person.telegram_id === state.me.telegram_id) return [];
  if (person.role !== "user" && !state.me.is_admin) return [];
  if (person.status === "pending") return ["approve", "reject"];
  if (person.status === "approved") return ["ban", "delete"];
  if (person.status === "rejected") return ["approve", "ban"];
  return state.me.is_admin ? ["restore"] : [];
}

function runPersonAction(person, action) {
  const name = personName(person);
  const endpoint = action === "restore" ? "approve" : action;
  const path = `/admin/users/${person.telegram_id}/${endpoint}`;
  const title = t(`webapp.admin.confirm.${action}.title`);
  const text = fill(`webapp.admin.confirm.${action}.text`, { name });
  if (!REASON_ACTIONS.includes(action)) {
    openConfirm({
      title,
      text,
      confirmLabel: t(`webapp.admin.action.${action}`),
      errorKey: "webapp.admin.error.action",
      doneKey: `webapp.admin.done.${action}`,
      run: () => api(path, { method: "POST", body: {} }),
    });
    return;
  }
  // The reason and the confirmation share one sheet: the submit button is the confirmation.
  const reason = textField(t("webapp.admin.reason"), "admin-reason", "", 255);
  reason.field = el("div", {}, [el("div", { className: "summary", text }), reason.field]);
  openBandForm({
    title,
    fields: [reason],
    submitLabel: t(`webapp.admin.action.${action}`),
    errorKey: "webapp.admin.error.action",
    doneKey: `webapp.admin.done.${action}`,
    run: ([value]) => api(path, { method: "POST", body: { reason: value || null } }),
  });
}

function openPersonSheet(person) {
  const buttons = personActions(person).map((action, index) => {
    const button = el("button", {
      className: index === 0 ? "btn" : "btn secondary",
      type: "button",
      text: t(`webapp.admin.action.${action}`),
    });
    button.addEventListener("click", () => runPersonAction(person, action));
    return button;
  });
  if (person.telegram_username && person.telegram_id !== state.me.telegram_id) {
    const write = el("button", { className: "btn secondary", type: "button", text: t("webapp.bands.write") });
    write.addEventListener("click", () => openTelegramUser(person.telegram_username));
    buttons.push(write);
  }
  const close = el("button", { className: "btn secondary", type: "button", text: t("webapp.card.close") });
  close.addEventListener("click", closeSheet);
  buttons.push(close);
  openSheet([
    el("h2", { text: personName(person) }),
    detailRow(t("webapp.profile.username"), person.telegram_username ? `@${person.telegram_username}` : ""),
    detailRow(t("webapp.profile.study_group"), person.study_group),
    detailRow(t("webapp.profile.club_group"), person.group ? person.group.title : ""),
    detailRow(t("webapp.profile.status"), t(`webapp.admin.status.${person.status}`)),
    detailRow(t("webapp.profile.roles"), person.role === "user" ? "" : t(`webapp.admin.role.${person.role}`)),
    detailRow(t("webapp.admin.reason_label"), person.moderation_reason),
    detailRow(t("webapp.admin.created"), shortDate(person.created_at.slice(0, 10))),
    personActions(person).length ? null : el("div", { className: "meta", text: t("webapp.admin.no_actions") }),
    sheetActions(...buttons),
  ]);
}

function personRow(person) {
  const username = person.telegram_username ? `@${person.telegram_username}` : "";
  const groupTitle = person.group ? person.group.title : "";
  const badges = [t(`webapp.admin.status.${person.status}`)];
  if (person.role !== "user") badges.push(t(`webapp.admin.role.${person.role}`));
  const row = el("div", { className: "member", role: "button", tabindex: "0" }, [
    el("div", {}, [
      el("div", { className: "member-name", text: personName(person) }),
      el("div", { className: "meta", text: [username, person.study_group, groupTitle].filter(Boolean).join(" · ") }),
    ]),
    el("div", { className: "badges" }, badges.map((text) => el("span", { className: "badge", text }))),
  ]);
  row.addEventListener("click", () => openPersonSheet(person));
  return row;
}

function peopleList(people, emptyText) {
  return people.length ? el("div", { className: "card" }, people.map(personRow)) : notice(emptyText);
}

function adminSegments(tabs, pending) {
  return el("div", { className: "segments" }, tabs.map((tab) => {
    const button = el("button", { className: `segment${tab === state.adminTab ? " active" : ""}`, type: "button" }, [
      t(`webapp.admin.tab.${tab}`),
      tab === "requests" && pending ? el("span", { className: "badge-count", text: String(pending) }) : null,
    ]);
    button.addEventListener("click", () => { state.adminTab = tab; render(); });
    return button;
  }));
}

function usersPath() {
  const params = new URLSearchParams();
  if (state.adminStatus) params.set("status", state.adminStatus);
  if (state.adminQuery.trim()) params.set("q", state.adminQuery.trim());
  const query = params.toString();
  return `/admin/users${query ? `?${query}` : ""}`;
}

function usersControls(list) {
  const search = el("input", { type: "search", maxlength: "100", autocomplete: "off", placeholder: t("webapp.admin.search") });
  search.value = state.adminQuery;
  const status = el("select", {}, [
    el("option", { value: "", text: t("webapp.admin.filter.all") }),
    ...USER_STATUSES.map((value) => el("option", { value, text: t(`webapp.admin.status.${value}`) })),
  ]);
  status.value = state.adminStatus;
  const reload = async () => {
    const mine = ++state.adminSearchToken;
    try {
      const data = await api(usersPath());
      if (mine === state.adminSearchToken) list.replaceChildren(peopleList(data.users, t("webapp.admin.empty.users")));
    } catch (_) {
      if (mine === state.adminSearchToken) list.replaceChildren(notice(t("webapp.error.load"), true));
    }
  };
  let timer = null;
  search.addEventListener("input", () => {
    state.adminQuery = search.value;
    clearTimeout(timer);
    timer = setTimeout(reload, SEARCH_DEBOUNCE_MS);
  });
  status.addEventListener("change", () => { state.adminStatus = status.value; reload(); });
  return el("div", { className: "form admin-filters" }, [search, status]);
}

function openRoleForm(target) {
  const field = textField(t("webapp.admin.roles.target"), "role-target", "", 100);
  field.required = true;
  openBandForm({
    title: t(target.addTitleKey),
    fields: [field],
    submitLabel: t("webapp.admin.roles.add"),
    errorKey: "webapp.admin.error.action",
    doneKey: "webapp.admin.roles.added",
    run: ([value]) => api(target.path, { method: "POST", body: { target: value, enabled: true } }),
  });
}

function roleRow(person, path) {
  const username = person.telegram_username ? `@${person.telegram_username}` : "";
  const children = [
    el("div", {}, [
      el("div", { className: "member-name", text: personName(person) }),
      el("div", { className: "meta", text: username }),
    ]),
  ];
  if (person.telegram_id !== state.me.telegram_id) {
    const remove = el("button", { className: "btn secondary small", type: "button", text: t("webapp.admin.roles.remove") });
    remove.addEventListener("click", () => openConfirm({
      title: t("webapp.admin.roles.remove_title"),
      text: fill("webapp.admin.roles.remove_text", { name: personName(person) }),
      confirmLabel: t("webapp.admin.roles.remove"),
      errorKey: "webapp.admin.error.action",
      doneKey: "webapp.admin.roles.removed",
      run: () => api(path, { method: "POST", body: { target: String(person.telegram_id), enabled: false } }),
    }));
    children.push(remove);
  }
  return el("div", { className: "member" }, children);
}

function roleSection(title, people, target) {
  const rows = people.length
    ? people.map((person) => roleRow(person, target.path))
    : [el("div", { className: "meta", text: t("webapp.admin.empty.roles") })];
  const add = el("button", { className: "btn secondary", type: "button", text: t("webapp.admin.roles.add") });
  add.addEventListener("click", () => openRoleForm(target));
  return el("div", { className: "card" }, [el("h2", { text: title }), ...rows, sheetActions(add)]);
}

function roleSections(staff) {
  return [
    roleSection(t("webapp.admin.roles.admins"), staff.admins, { path: "/admin/staff/admin", addTitleKey: "webapp.admin.roles.add_admin" }),
    roleSection(t("webapp.admin.roles.dsvr"), staff.dsvr, { path: "/admin/staff/dsvr", addTitleKey: "webapp.admin.roles.add_dsvr" }),
    ...staff.group_admins.map((item) =>
      roleSection(`${t("webapp.admin.roles.group_admins")}: ${item.group.title}`, item.admins, {
        path: `/admin/groups/${item.group.id}/admins`,
        addTitleKey: "webapp.admin.roles.add_group_admin",
      })
    ),
  ];
}

async function renderAdmin(screen, token) {
  document.getElementById("meta").textContent = "";
  if (!canAdmin()) {
    screen.replaceChildren(notice(t("webapp.admin.not_allowed"), true));
    return;
  }
  const tabs = state.me.is_admin ? ADMIN_TABS : ADMIN_TABS.filter((tab) => tab !== "roles");
  if (!tabs.includes(state.adminTab)) state.adminTab = tabs[0];
  if (state.adminTab === "roles") {
    const [staff, pending] = await Promise.all([api("/admin/staff"), api("/admin/users?status=pending")]);
    if (token !== state.renderToken) return;
    screen.replaceChildren(adminSegments(tabs, pending.counts.pending), ...roleSections(staff));
  } else if (state.adminTab === "requests") {
    const data = await api("/admin/users?status=pending");
    if (token !== state.renderToken) return;
    screen.replaceChildren(adminSegments(tabs, data.counts.pending), peopleList(data.users, t("webapp.admin.empty.requests")));
  } else {
    const data = await api(usersPath());
    if (token !== state.renderToken) return;
    const list = el("div", {}, [peopleList(data.users, t("webapp.admin.empty.users"))]);
    screen.replaceChildren(adminSegments(tabs, data.counts.pending), usersControls(list), list);
  }
}

/* ---------- profile ---------- */

function notice(text, isError = false) {
  return el("div", { className: `notice${isError ? " error" : ""}`, text });
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
  if (route !== "schedule") {
    state.selection = null;
    renderSelectionBar();
  }
  try {
    if (route === "me") renderProfile(screen);
    else if (route === "mine") await renderMine(screen, token);
    else if (route === "bands") await renderBands(screen, token);
    else if (route === "admin") await renderAdmin(screen, token);
    else await renderSchedule(screen, token);
  } catch (error) {
    if (token === state.renderToken) {
      screen.replaceChildren(notice(t("webapp.error.load"), true));
    }
  }
  if (route === "schedule" && token === state.renderToken) {
    // While the app is in the background the timer idles; the visibilitychange handler refreshes on return.
    state.refreshTimer = setTimeout(() => { if (!document.hidden && !state.closeSheet) render(); }, REFRESH_MS);
  }
}

async function main() {
  state.me = await loadMe();
  await loadStrings(state.me ? state.me.language : telegramLanguage());
  if (tg && tg.BackButton) {
    tg.BackButton.onClick(() => {
      if (state.closeSheet) closeSheet();
      else location.hash = "#/schedule";
    });
  }
  // The bot's web_app buttons cannot carry a start_param, so they pass ?screen=<route> and ?join=<code> instead.
  // A direct link like t.me/<bot>/<app>?startapp=join_<code> arrives as start_param.
  const query = new URLSearchParams(location.search);
  const startParam = tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param;
  const joinMatch = canBook() ? JOIN_PARAM.exec(startParam || (query.has("join") ? `join_${query.get("join")}` : "")) : null;
  if (ADMIN_TABS.includes(query.get("tab"))) state.adminTab = query.get("tab");
  if (joinMatch) location.hash = "#/bands";
  else if (ROUTES.includes(query.get("screen")) && !ROUTES.includes(location.hash.replace(/^#\/?/, "").split("/")[0])) {
    location.hash = `#/${query.get("screen")}`; // Telegram fills the hash with its own launch data, so only a route counts
  }
  window.addEventListener("hashchange", () => { closeSheet(); render(); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && currentRoute() === "schedule" && !state.closeSheet) render();
  });
  await render();
  if (joinMatch) openJoinForm(joinMatch[1]);
}

main();
