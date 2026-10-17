# Admin Users Screens (Mini App, stage 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move registration requests, user moderation and role management from the bot into a Mini App "Админ" screen; the bot only opens it.

**Architecture:** A new thin router `app/api/routes/webapp_admin.py` (`/webapp/api/admin/*`, signed `initData` auth) calls the existing moderation services in `services/users.py` and `services/groups.py`. User-facing notifications (approved / rejected / admin granted / admin revoked / group admin granted) move from bot handlers into those services via `outbox.queue_notification`, published after commit. The front end is a new "admin" route in the vanilla-JS `app.js`; the bot's admin menu gets `web_app` buttons and the old FSM flows are deleted.

**Tech Stack:** FastAPI, SQLAlchemy async, aiogram 3, pytest (SQLite, `asyncio_mode=auto`), vanilla JS.

**Spec:** `docs/superpowers/specs/2026-10-17-admin-users-screens-design.md`

## Global Constraints

- Business rules stay in `app/services/`; routes are thin. Permission rules are not rewritten (CLAUDE.md, spec "Что не меняется").
- Webapp routes authenticate with `X-Telegram-Init-Data` only (no `X-Internal-Token`): reads use `current_webapp_user` (24 h), mutations use `fresh_webapp_user` (1 h).
- `AppError` subclasses already map to HTTP codes in `app/main.py` (`ForbiddenError` 403, `NotFoundError` 404, `ConflictError` 409, `UnauthorizedError` 401); do not add a second error mapping.
- Bot never touches the DB; it only calls `BackendClient`.
- No schema change, so no `ALTER` in `create_db_and_tables()`.
- Every mutation keeps going through the existing services that call `log_action` (audit).
- Mini App strings live under `webapp.admin.*` in `app/locales/{ru,en,kz}.json`. **All three files get the same keys**: `tests/test_webapp.py::test_every_language_has_the_same_mini_app_strings` asserts equal key sets (the spec said "ru only"; that would fail this test).
- Time: use `core/timezone.now_local()`, not naive `datetime.now()`.
- Bash heredocs mangle backslashes on this setup; use Write/Edit for any file with `\`.
- Process (user's standing workflow): before each task show its mini-spec, run `pytest` green, commit the task alone, then state the next task and its recommended effort. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Failure modes the spec implies but no task's happy path covers; each has a test in the owning task:

1. Approving someone twice (two admins, or a stale "Одобрить" button in an old chat message) must not error or notify twice. Owner: Task 1.
2. A banned or deleted user: a group admin tries to restore them via `approve` and must get 403; a main admin may. Owner: Task 1.
3. A user with `group_id=None` is invisible to group admins (list, card and every action) and visible to main admins. Owner: Task 1.
4. A `q` search containing SQL wildcards (`%`, `_`) or only whitespace must match literally / behave as no filter, not as a wildcard. Owner: Task 1.
5. Granting or revoking a role to a non-existent `@username` returns 404 with a readable message, and `reason` longer than 255 chars is rejected with 422 instead of a DB error. Owner: Task 1.

## File Structure

- Create `app/api/routes/webapp_admin.py` — admin Mini App endpoints and the access dependency.
- Create `app/schemas/webapp_admin.py` — request bodies.
- Modify `app/services/users.py`, `app/services/groups.py` — queue notifications; add `search_users` and `status_counts`.
- Modify `app/api/routes/users.py`, `app/api/routes/groups.py` — call `outbox.commit_and_publish` after the changed services (internal API used by the bot).
- Modify `app/main.py` — include the new router.
- Modify `app/webapp/static/app.js`, `app.css` — admin route and screens.
- Modify `app/locales/ru.json`, `en.json`, `kz.json` — `webapp.nav.admin`, `webapp.admin.*`.
- Modify `app/bot/keyboards.py`, `app/bot/handlers.py`, `app/bot/handlers_groups.py`, `app/bot/states.py`, `app/bot/api_client.py` — web_app buttons, delete old flows.
- Create `tests/test_webapp_admin.py` — API and notification tests. Extend `tests/test_webapp.py` for keyboards.

---

### Task 1: Admin API and notifications in services

**Files:**
- Create: `app/api/routes/webapp_admin.py`, `app/schemas/webapp_admin.py`
- Modify: `app/services/users.py`, `app/services/groups.py`, `app/api/routes/users.py`, `app/api/routes/groups.py`, `app/main.py:10,115`, `app/bot/handlers.py` (the sends at `inline_registration_approve`, `inline_registration_reject`, `approve_user_save`, `add_admin_save`, `remove_admin_save`), `app/bot/handlers_groups.py` (`groups_add_finish`)
- Test: `tests/test_webapp_admin.py`

**Interfaces:**
- Consumes: `current_webapp_user`, `fresh_webapp_user` (`app/api/webapp_auth.py`); `users.list_users_by_status`, `approve_user`, `reject_user`, `ban_user`, `soft_delete_user`, `promote_to_admin`, `demote_admin`, `set_dsvr_role`, `list_admins`, `get_user_for_requester`; `groups.add_group_admin`, `remove_group_admin`, `list_group_admins`; `outbox.queue_notification(session, telegram_id, text)`, `outbox.commit_and_publish(session)`; tests: `client`, `tg` from `tests.test_webapp`, fixtures `make_user`, `session`, `published`.
- Produces (all under prefix `/webapp/api/admin`; person payload `person(user)` below):
  - `GET /users?status=<str>&q=<str>` → `{"users": [person], "counts": {"pending": int, "approved": int, "rejected": int, "banned": int, "deleted": int}}`; counts are computed over the same permission-scoped set as `users`, before the `status`/`q` filters.
  - `GET /users/{telegram_id}` → `person`
  - `POST /users/{telegram_id}/approve` (no body), `/reject`, `/ban`, `/delete` (body `{"reason": str | null}`, max 255) → `person`
  - `GET /staff` (main admin only) → `{"admins": [person], "dsvr": [person], "group_admins": [{"group": {"id","slug","title"}, "admins": [person]}]}`
  - `POST /staff/admin`, `POST /staff/dsvr` (body `{"target": str, "enabled": bool}`, main admin only) → `person`
  - `POST /groups/{group_id}/admins` (body `{"target": str, "enabled": bool}`, main admin only) → `person`
  - `person` = `{"telegram_id": int, "telegram_username": str|null, "first_name": str|null, "last_name": str|null, "study_group": str|null, "group": {"id","slug","title"}|null, "role": str, "status": str, "moderation_reason": str|null, "created_at": iso str}`
  - `app.schemas.webapp_admin.WebAppModerate(reason: str | None = Field(default=None, max_length=255))` and `WebAppRoleChange(target: str = Field(min_length=1, max_length=100), enabled: bool = True)`
  - `users.search_users(session, admin_telegram_id: int, status: str | None, query: str | None) -> tuple[list[User], dict[str, int]]`
  - Notification texts (Russian, exactly as the bot sends today): approve → `"Ваша регистрация одобрена. Теперь доступны все функции AITU Music Club Bot."`; reject → `"Ваша регистрация отклонена, но это не бан.\nПовторно подать заявку можно после: {retry_after}.\nКогда срок пройдет, отправьте /start."` (`retry_after` = `target.registration_retry_after` or `"позже"`); promote → `"Вам выдали права админа в AITU Music Club Bot."`; demote → `"С вас сняли права админа в AITU Music Club Bot."`; group admin added → `"Вас назначили админом группы. Откройте /start, чтобы увидеть админ-меню."`.

Spec clarification this task makes: the bot **already** sends a reject notice to the user (`inline_registration_reject`), so reject notification is moved into the service too (not "new"); ban and delete still send nothing.

- [ ] **Step 1: Write the failing tests in `tests/test_webapp_admin.py`**
  Import `client`, `tg` from `tests.test_webapp` (as `tests/test_webapp_bands.py` does). Add helper `async def staff(make_user, session)` returning `(main_admin, group_admin_music, pending_music, pending_other)`: main admin `make_user(role="admin")`, a `GroupAdmin(group_id=<music id>, user_id=<user id>)` row for an approved user with `group_slug="music"`, a `status="pending"` user in `music`, and one in the group of the second slug in `models.GROUP_SLUGS`. Tests (names exact):
  - `test_admin_endpoints_reject_everyone_but_admins`: plain approved user, DSVR (`role="dsvr"`) and pending user get 403 on `GET /webapp/api/admin/users`; no header gets 401.
  - `test_group_admin_sees_only_their_group_and_counts_match`: group admin's `GET /users` returns only `pending_music` (+ themselves/other members of `music`), `counts["pending"] == 1`; main admin sees both pending users, `counts["pending"] == 2`.
  - `test_users_filter_by_status_and_search`: `?status=pending`; `?q=<part of last_name>`; `?q=<@username without @>`; `?q=<telegram_id>`; `?q=%` returns `[]` (literal match), `?q=%20%20` behaves as no filter.
  - `test_approve_notifies_once_and_is_idempotent`: approve → `status == "approved"`, `published` has exactly one `{"type":"notify","telegram_id":<target>,"text":<approve text>}`; approving a second time returns 200 with `status == "approved"` and `len(published)` is still 1 (a repeat on an already-approved user queues no notification).
  - `test_reject_ban_delete_store_reason_and_only_reject_notifies`: reject with `{"reason":"не студент"}` → `status == "rejected"`, `moderation_reason == "не студент"`, one reject notification; ban and delete add no `notify` events; `reason` of 256 chars → 422.
  - `test_group_admin_cannot_touch_staff_other_groups_or_themselves`: group admin gets 403 on `ban` for a main admin, for `pending_other`, and for themselves; and 403 on `/staff`, `/staff/admin`, `/staff/dsvr`, `/groups/{id}/admins`.
  - `test_group_admin_cannot_restore_a_banned_user_but_main_admin_can`: banned `music` user: group admin `approve` → 403, main admin → 200.
  - `test_users_without_a_group_are_invisible_to_group_admins`: user with `group_slug=None, status="pending"`: absent from the group admin's list, `GET /users/{id}` → 403 or 404 (whatever `get_user_for_requester` raises), present for the main admin.
  - `test_initial_admin_and_self_are_protected`: monkeypatch `settings.initial_admin_ids` to `{target.telegram_id}`; `ban` → 403; main admin `ban` of own id → 403; `POST /staff/admin` with `enabled=false` for self → 403.
  - `test_stale_init_data_reads_but_cannot_change`: `init_data(..., auth_date=<2 h ago>)` (see `tests/test_webapp_auth.py` for the helper's signature) → `GET /users` 200, `POST .../approve` 401.
  - `test_staff_lists_and_role_changes`: main admin `POST /staff/admin {"target": "@<username>", "enabled": true}` → role `admin` and one promote notification; `enabled=false` → role `user` and one demote notification; `POST /staff/dsvr` true/false; `POST /groups/{id}/admins` true adds a `GroupAdmin` row and one group-admin notification, false removes it; `GET /staff` reflects all three lists.
  - `test_unknown_target_is_404_and_readable`: `POST /staff/admin {"target":"@nobody","enabled":true}` → 404 and non-empty `detail`.
  - `test_notifications_respect_the_opt_out`: target with `notifications_enabled=False` is approved, audit row written (`AuditLog` count +1), `published` has no `notify` event.

- [ ] **Step 2: Run to verify they fail**
  Run: `pytest tests/test_webapp_admin.py -q` — Expected: FAIL (404 on every route / import errors).

- [ ] **Step 3: Services — notifications and search**
  In `app/services/users.py`: after `log_action` and before `commit` in `approve_user` (only if `target.status` was not already `approved` before the change), `reject_user`, `promote_to_admin`, `demote_admin`, call `outbox.queue_notification(session, target.telegram_id, <text>)` guarded by `target.notifications_enabled`. Add `search_users(...)` reusing the scoping in `list_users_by_status` (extract the scoped `select(User)` into a private helper used by both): `q` is stripped, empty = no filter, otherwise case-insensitive literal match (escape `%`/`_`/`\\` with `ilike(..., escape="\\")`) against first name, last name, `telegram_username` (leading `@` stripped) and `str(telegram_id)`; counts group the scoped set by `status` and include every status key with 0 defaults. In `app/services/groups.py` `add_group_admin` queues the group-admin text the same way. Add `import outbox` lazily inside functions if a circular import appears (`outbox` is imported lazily elsewhere in services).

- [ ] **Step 4: Internal routes publish**
  In `app/api/routes/users.py` (`approve_user`, `promote_admin`, `demote_admin`, `reject_user`) and `app/api/routes/groups.py` (`add_group_admin`): call `await outbox.commit_and_publish(session)` after the service returns, so bot-initiated actions deliver the queued notifications. Delete the now-duplicate `bot.send_message` blocks listed under Files from the bot handlers (approve in `inline_registration_approve`, reject in `inline_registration_reject`, `approve_user_save`, `add_admin_save`, `remove_admin_save`, `groups_add_finish`); leave the admin-notice message edits (`update_registration_notice_messages`) untouched.

- [ ] **Step 5: Implement `webapp_admin.py`**
  `router = APIRouter(prefix="/webapp/api/admin", tags=["webapp-admin"])`; helpers `async def admin_reader(user=Depends(current_webapp_user), session=Depends(get_session)) -> User` and `admin_writer` (same with `fresh_webapp_user`) that raise `ForbiddenError("Admin access required")` unless `is_global_admin(user)` or `await administered_group_ids(session, user)` is non-empty; `main_admin_writer` additionally requires `is_global_admin`. Every route calls the services with `user.telegram_id`, then `await outbox.commit_and_publish(session)` on mutations, and returns `person(...)` built by a module-level `async def person(session, user) -> dict`. `/staff` combines `users.list_admins`, `User` rows with role `dsvr`, and `groups.list_group_admins` per group. Role toggles dispatch to `promote_to_admin`/`demote_admin`, `set_dsvr_role(..., enabled)`, `add_group_admin`/`remove_group_admin`. Register in `app/main.py`: add `webapp_admin` to the import on line 10 and `app.include_router(webapp_admin.router)` after `webapp.router`.

- [ ] **Step 6: Run the new tests, then the whole suite**
  Run: `pytest tests/test_webapp_admin.py -q` then `pytest -q` and `python -m compileall -q app` — Expected: all PASS, no output from compileall.

- [ ] **Step 7: Commit**
  ```bash
  git add app tests docs
  git commit -m "Add Mini App admin API for requests, users and roles; move user notifications into services"
  ```

---

### Task 2: Mini App admin screens

**Files:**
- Modify: `app/webapp/static/app.js` (`ROUTES` line 7, `renderChrome` line 150, `render` line 949, `main` line 975), `app/webapp/static/app.css`, `app/locales/ru.json`, `app/locales/en.json`, `app/locales/kz.json`
- Test: `tests/test_webapp.py` (locale parity test already exists; add the two below)

**Interfaces:**
- Consumes: the Task 1 endpoints and payload shapes; existing helpers in `app.js`: `api(path, options)`, `el`, `t`, `fill(key, values)`, `openSheet`, `closeSheet`, `openBandForm({title, fields, submitLabel, errorKey, doneKey, run})` (run receives the trimmed field values in order and rejects with the ApiError to keep the sheet open), `openConfirm({title, text, confirmLabel, errorKey, doneKey, run})`, `textField(label, id, value, maxlength)`, `sheetActions`, `notice`, `toast`, `state.me` (`is_admin`, `administered_groups`, `registered`, `status`).
- Produces: route `admin`; `function canAdmin()` returns `Boolean(state.me && state.me.registered && state.me.status === "approved" && (state.me.is_admin || state.me.administered_groups.length))`; `async function renderAdmin(screen, token)`; segment state `state.adminTab ∈ {"requests","users","roles"}`; the route also honours `?tab=` in `main()`.

- [ ] **Step 1: Write the failing tests**
  In `tests/test_webapp.py`: `test_admin_strings_exist_in_every_language` — for `ru`, `en`, `kz` the keys `webapp.nav.admin`, `webapp.admin.tab.requests`, `webapp.admin.tab.users`, `webapp.admin.tab.roles` exist and are non-empty (parity of the full set is covered by the existing test); `test_app_js_only_uses_defined_admin_strings` — regex `t\("(webapp\.admin\.[a-z_.]+)"` and `` t\(`(webapp\.admin\.[^`$]+)` `` over `app.js` (read as UTF-8), and assert each literal key exists in `ru.json`; dynamic keys use the documented prefixes `webapp.admin.status.<status>` and `webapp.admin.role.<role>` which must exist for all of `pending, approved, rejected, banned, deleted` and `user, admin, dsvr`.

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_webapp.py -q -k admin` — Expected: FAIL (keys missing).

- [ ] **Step 3: Locale strings**
  Add to all three locale files, same keys: `webapp.nav.admin`, `webapp.admin.tab.requests|users|roles`, `webapp.admin.empty.requests|users|search`, `webapp.admin.search` (placeholder), `webapp.admin.filter.all`, `webapp.admin.status.<5 statuses>`, `webapp.admin.role.user|admin|dsvr|group_admin`, action labels `webapp.admin.action.approve|reject|ban|delete|restore`, reason prompt `webapp.admin.reason` ("Причина (необязательно)"), confirm titles/texts `webapp.admin.confirm.reject|ban|delete` with `{name}` placeholder, done toasts `webapp.admin.done.<action>`, error `webapp.admin.error.action`, roles section `webapp.admin.roles.admins|dsvr|group_admins`, role-form labels `webapp.admin.roles.target` ("@username или Telegram ID"), `webapp.admin.roles.add_admin|remove_admin|add_dsvr|remove_dsvr|add_group_admin|remove_group_admin`, and `webapp.admin.not_allowed`. Russian is the source text; give real `en` and `kz` translations.

- [ ] **Step 4: Front end**
  `ROUTES` gains `"admin"`; `renderChrome` shows the "admin" tab only when `canAdmin()` (filter the list; `currentRoute()` falls back to `schedule` for a viewer without access). `renderAdmin` draws a segmented control (Заявки with the pending count badge / Пользователи / Роли; Роли only if `state.me.is_admin`), then:
  - Заявки: `GET /admin/users?status=pending`, one card per person (name, `@username`, study group, club group, created date); tap opens the person sheet.
  - Пользователи: search `<input>` (debounce 300 ms, re-fetches with `q`) and a status `<select>` (`all` + 5 statuses); list of person rows.
  - Роли: `GET /admin/staff`; three sections (admins, ДСВР, group admins per group) with a "снять" button on each row and an "add" button per section that opens `openBandForm` with a `textField` for the target; the group section's add button carries the `group_id`.
  - Person sheet (`openPersonSheet(person)`): detail rows plus only the actions valid for that person and viewer: `pending` → approve, reject; `approved` → ban, delete (reject hidden); `rejected` → approve, ban; `banned`/`deleted` → restore (`approve`) shown to main admins only; for staff roles (`role !== "user"`) and for own `telegram_id` show no moderation actions for non-main admins and for self. Approve runs `api(..., {method:"POST"})` through `openConfirm`; reject/ban/delete first collect an optional reason with `openBandForm` (one `textField`, maxlength 255) and then confirm with `openConfirm`, passing `{reason}` as JSON.
  - Errors surface the backend `detail` via `sessionAwareMessage(failure, "webapp.admin.error.action")` (already used by the forms); the list refreshes via the existing `render()` call inside the form helpers. Empty states use `notice(...)`.
  - CSS: reuse `.card`, `.row`, `.badges`, `.actions`, `.notice`; add only a `.segments` control and `.badge-count` if no existing class fits.
  - Do not add auto-refresh to this route.

- [ ] **Step 5: Run tests** — `pytest -q` — Expected: all PASS.

- [ ] **Step 6: Verify in the running app**
  Start backend (`uvicorn app.main:app` with `ENVIRONMENT=local`, a local `INTERNAL_API_TOKEN`, SQLite or the compose Postgres) and exercise the screen in a browser with a signed `initData` for a seeded main admin and a seeded group admin (no Telegram client is needed: serve with a small script that sets `window.Telegram.WebApp.initData` using `tests/test_webapp_auth.py::init_data`). Check: tab visibility per role, approve and reject with reason, search, status filter, a 403 message shown in the sheet, mobile width 360 px with no horizontal scroll. Record in the commit message body exactly what was and was not exercised.

- [ ] **Step 7: Commit**
  ```bash
  git add app tests
  git commit -m "Mini App: admin screen with requests, users and roles"
  ```

---

### Task 3: Bot opens the admin screen; remove the old flows

**Files:**
- Modify: `app/bot/keyboards.py` (`admin_inline_keyboard`, `staff_inline_keyboard`, `admin_keyboard`, delete `admin_users_keyboard`, `admin_users_manual_keyboard`, `admin_admins_keyboard`, `admin_user_list_keyboard`, `admin_user_detail_keyboard` if unused after the edit), `app/bot/handlers.py` (`admin_menu_keyboard_for` + its three callers, `LEGACY_MINI_APP_SCREENS`, the handlers listed below), `app/bot/states.py`, `app/bot/api_client.py`
- Test: `tests/test_webapp.py` (next to `test_rehearsals_and_bands_menus_open_the_mini_app`)

**Interfaces:**
- Consumes: `mini_app_button(text, base_url, screen)` / `mini_app_url` (`app/bot/keyboards.py:54-66`; screen `"admin"` yields `?screen=admin`, add an optional `tab` through the existing `join_code`-style query only if needed: extend `mini_app_url(base_url, screen="schedule", join_code=None, tab=None)` adding `tab=<value>`), `get_webapp_base_url()` (`app/bot/webapp_url.py`), `send_mini_app_notice(message, text, button, screen)`.
- Produces: `admin_inline_keyboard(webapp_base_url: str | None = None)`, `staff_inline_keyboard(is_group_admin: bool, is_dsvr: bool, webapp_base_url: str | None = None)`; `async def admin_menu_keyboard_for(user: dict)` (now async, resolves the base URL itself); the webapp router reads `?tab=` (Task 2 `main()` handles it).

- [ ] **Step 1: Write the failing tests** in `tests/test_webapp.py`:
  - `test_admin_menus_open_the_mini_app`: with base `"https://club.example/"`, `_web_app_urls(keyboards.admin_inline_keyboard(base))` contains `https://club.example/webapp/schedule?screen=admin&tab=requests` and `...&tab=roles`; `staff_inline_keyboard(True, False, base)` contains the `tab=requests` URL and no `tab=roles`; `staff_inline_keyboard(False, True, base)` contains no admin web_app URL.
  - `test_admin_menus_drop_the_moderation_callbacks`: for the same keyboards `_callbacks(...)` contains none of `admin_section:users`, `admin_section:admins`; without a base URL the keyboards render and still contain `menu:back`/`admin:back` and the untouched sections (`admin_section:schedule`, `groups:list`).
  - `test_stale_moderation_buttons_are_routed_to_the_legacy_handler`: `LEGACY_MINI_APP_SCREENS["admin"]` exists and its prefixes include `admin_section:users`, `admin_section:admins`, `admin_users:`, `admin_user:`, `admin_user_action:`, `admin:pending_users`, `admin:approve_user`, `admin:reject_user`, `admin:ban_user`, `admin:delete_user`, `admin:add_admin`, `admin:remove_admin`.

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_webapp.py -q -k "admin_menus or stale_moderation"` — Expected: FAIL.

- [ ] **Step 3: Keyboards and menu builder**
  Implement the signatures above: "Заявки" → `mini_app_button("Заявки", base, "admin", tab="requests")`, "Админы" → `mini_app_button("Админы", base, "admin", tab="roles")` (main admin only); when `webapp_base_url` is falsy the button is omitted (as `bands_menu_keyboard` does). `admin_menu_keyboard_for` becomes async; update its callers (`handlers.py` around lines 947, 1212, 2119) and the two `admin_inline_keyboard()` fallbacks in `inline_admin_section`/line 2550 to pass the base URL. Remove the six moderation buttons from the reply `admin_keyboard()` ("Заявки пользователей", "Одобрить пользователя", "Отклонить пользователя", "Забанить пользователя", "Удалить пользователя", "Добавить админа"), keeping the rest.

- [ ] **Step 4: Delete old handlers and states, add legacy routing**
  Delete from `handlers.py`: `inline_pending_users`, the `users` and `admins` branches of `inline_admin_section`, `inline_admin_users_manual`, `inline_admin_users_list`, `inline_admin_user_detail`, `inline_admin_user_action`, the four `inline_*_user_start`/`approve_user_start`/`add_admin_start`/`remove_admin_start` starters, `approve_user_save`, `add_admin_save`, `remove_admin_save`, `reject_user_save`, `ban_user_save`, `delete_user_save`, `pending_users`, the reply-text starters for the six buttons, then the now-unused `format_admins_list`, `format_user_card` (if no other caller) and imports. Delete `AdminFlow.approve_user|reject_user|ban_user|delete_user|add_admin|remove_admin` from `states.py` and `BackendClient` methods that have no caller left (`list_admins`, `promote_admin`, `demote_admin`, `get_user`/`moderate_user` are still used by `registration:*` callbacks — keep those). Keep `inline_registration_approve/reject`, `notify_admins_about_registration` and the whole `groups:*` flow untouched. Add `"admin"` to `LEGACY_MINI_APP_SCREENS` with the prefixes from the test, button `"⚙️ Админка"`, text `"Заявки, пользователи и роли теперь в мини-приложении."`, and add a message handler for the six removed reply-keyboard texts that clears FSM state and calls `send_mini_app_notice(message, ..., "⚙️ Админка", "admin")`. Make sure the legacy handler stays registered before the `admin_section:` handler (it is defined earlier in the file).

- [ ] **Step 5: Verify**
  Run: `python -m compileall -q app` (no output), `pytest -q` (all PASS), then `python -c "import app.bot.handlers, app.bot.handlers_groups"` from the repo root (no ImportError). Grep for leftovers: `rg "AdminFlow\.(approve_user|reject_user|ban_user|delete_user|add_admin|remove_admin)|admin_users_keyboard|admin_admins_keyboard" app tests` returns nothing.

- [ ] **Step 6: Update docs**
  In `CLAUDE.md` extend the bot bullet ("Booking rehearsals and managing bands happen only in the Mini App…") with the admin requests/users/roles screens and the `/webapp/api/admin/*` routes; add the same one-liner to `README.md` where bot admin features are described.

- [ ] **Step 7: Commit**
  ```bash
  git add app tests CLAUDE.md README.md
  git commit -m "Move admin requests, users and roles from the bot to the Mini App"
  ```

---

## Self-Review

- **Spec coverage:** API table → Task 1; notifications (including the `registration:approve` duplicate check) → Task 1 steps 3-4; Mini App tabs/sheets/reasons/permissions → Task 2; bot buttons, deleted FSM flows, `legacy_flow_button` → Task 3; edge cases → Task 1 tests (idempotent approve, stale `initData`, 403/404 messages, no group, opt-out) and Task 2 step 6 (empty states, mobile). Out-of-scope items remain untouched (`groups:*` bot flow, other admin sections).
- **Deviations from the spec (call out to the user):** (1) en/kz strings are added, because the existing parity test requires equal key sets; (2) reject notification is kept (moved into the service) because the bot already sends it; (3) group-admin grant notification moves too; (4) the bot's `groups:*` management flow and `registration:*` inline buttons stay as they are.
- **Type consistency:** `person` shape, `WebAppModerate`, `WebAppRoleChange`, `search_users` and `admin_menu_keyboard_for` signatures are used identically across tasks; Task 2's `?tab=` and Task 3's `mini_app_url(..., tab=)` match.
- **Proportion:** signatures and test assertions only; no function bodies except where behavior is fixed by the spec (texts).
