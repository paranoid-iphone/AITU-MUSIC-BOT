# Настраиваемые роли — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Админ создаёт роли в Mini App, отмечает права из каталога и назначает роль на весь клуб или на одну группу; старые admin / ДСВР / админ группы становятся обычными ролями с теми же правами.

**Architecture:** Три таблицы (`roles`, `role_permissions`, `role_assignments`) и единая проверка `has_permission(session, user, perm, group_id)` в `services/permissions.py`. На время перевода ~116 мест проверка читает ещё и старые `users.role` / `group_admins` («мост»), поэтому каждый коммит рабочий; мост и старая схема удаляются в конце.

**Tech Stack:** Python, SQLAlchemy async, Alembic, FastAPI, aiogram, vanilla-JS Mini App, pytest (SQLite).

**Spec:** `docs/superpowers/specs/2026-10-20-custom-roles-design.md` (читать вместе с планом).

## Global Constraints

- Каталог прав ровно такой (ключ: область): `users.moderate`: group, `bands.manage`: group, `bookings.manage`: group, `bookings.priority`: club, `schedule.history`: group, `audit.view`: club, `settings.manage`: club, `events.manage`: club, `roles.manage`: club.
- Стартовые роли: «Админ» (`is_system`, строк в `role_permissions` нет, имеет весь каталог), «ДСВР» = `bookings.priority`, `bookings.manage`, `schedule.history`; «Админ группы» = `users.moderate`, `bands.manage`, `bookings.manage`, `schedule.history`. Имя роли до 60 символов.
- Отказ в доступе: `ForbiddenError("Admin access required")` (сообщение уже переведено). Новые сообщения `ServiceError` добавляются в `_FIXED_MESSAGES` / `_NUMBERED_MESSAGES` (`services/errors.py`) и получают ключи `webapp.error.code.<code>` в `ru/en/kz` (`tests/test_error_codes.py` это проверяет).
- Журнал: действия `role.create|update|delete|assign|revoke`, категория `role` в `AUDIT_CATEGORIES` и в `ActionCategory` (`webapp_admin.py`). Уведомления: `outbox.queue_notification`, затем `outbox.commit_and_publish`.
- Миграции заморожены: ревизии не импортируют модели; id `0004_roles`, `0005_drop_legacy_roles`. Время только через `core/timezone.utcnow()` / `now_local()`.
- `pytest` без warnings. Файлы с обратными слэшами правятся инструментами Write/Edit, не heredoc.
- Рабочее правило пользователя: перед каждой задачей показать короткую спецификацию шага, после зелёных тестов закоммитить, затем назвать следующую задачу и рекомендуемый effort. Коммиты заканчиваются строкой `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Работать на отдельной ветке (`custom-roles`): коммиты между задачами 4 и 11 содержат мост и не предназначены для выкладки по одному.

## Review Focus

Случаи, которые спецификация подразумевает, но задачи не покрывают очевидно; тест каждого добавлен в указанную задачу.

1. Право «весь клуб» (`audit.view` и др.), выданное в назначении с группой, не действует (Задача 2).
2. Забаненный, отклонённый, удалённый или ожидающий пользователь с назначениями не имеет прав (Задача 2).
3. Повторное назначение той же роли той же области, особенно при `group_id=NULL`, где SQL уникальность не держит (Задача 3).
4. Пустое, из пробелов, длиннее 60 символов и повторяющееся (без учёта регистра) имя роли; неизвестный ключ права (Задачи 3, 7).
5. Админ удаляет роль, которую носит сам: удаление проходит, права пропадают со следующего запроса; последнего держателя «Админ» и начального админа лишить роли нельзя (Задача 3).

## File Structure

- Create `app/services/permission_catalog.py`: каталог и стартовые роли (константы, без БД).
- Create `app/services/roles.py`: сервис ролей (создание, правка, удаление, назначение, сидинг).
- Modify `app/services/permissions.py`: `has_permission` и родственные; в конце удаляются старые проверки и мост.
- Modify `app/db/models.py`, create `alembic/versions/0004_roles.py` и `0005_drop_legacy_roles.py`.
- Modify проверки в `services/{users,bands,groups,bookings,schedule_history,planning,admin,audit,events,move_requests}.py`, `api/routes/{webapp,webapp_admin}.py`, `app/bot/*`, `app/webapp/static/app.js`, `app/locales/*.json`.
- Tests: новые файлы `tests/test_roles_schema.py`, `test_permission_checks.py`, `test_roles_service.py`, `test_custom_role_moderation.py`, `test_custom_role_bookings.py`, `test_custom_role_settings.py`, `test_webapp_roles_api.py`.

---

### Task 1: Каталог, таблицы, миграция 0004

**Files:**
- Create: `app/services/permission_catalog.py`, `alembic/versions/0004_roles.py`
- Modify: `app/db/models.py` (после `Group`/`GroupAdmin`)
- Test: `tests/test_roles_schema.py`

**Interfaces:**
- Produces (`permission_catalog.py`): `ADMIN_ROLE_NAME = "Админ"`; `PERMISSIONS: dict[str, str]` (ключ → `"club"` | `"group"`); `DEFAULT_ROLES: dict[str, tuple[str, ...]]` с ключами «ДСВР», «Админ группы».
- Produces (`models.py`): `Role(id, name String(60) unique, is_system bool default False, created_at; permissions: list[RolePermission], assignments: list[RoleAssignment]` с `cascade="all, delete-orphan"`), `RolePermission(id, role_id FK ondelete CASCADE, permission String(60); unique (role_id, permission))`, `RoleAssignment(id, user_id FK users.id, role_id FK ondelete CASCADE, group_id FK groups.id nullable, created_at; relationships role, user, group)`.

- [ ] **Step 1: Тесты в `tests/test_roles_schema.py`** (временные SQLite-файлы, как в `test_migrations.py`):
  - `test_upgrade_seeds_default_roles`: после `upgrade_to_head` имена ролей = {«Админ», «ДСВР», «Админ группы»}, «Админ» с `is_system=1` и без строк в `role_permissions`, а права «ДСВР» и «Админ группы» равны `DEFAULT_ROLES` (так замороженный список миграции привязан к каталогу).
  - `test_upgrade_moves_legacy_staff_to_assignments`: `command.upgrade(config, "0003_schedule_change_booking_date")`, вставить через `exec_driver_sql` группу `music`, пользователей с `role` `admin`, `dsvr`, `user` (обязательные колонки: `telegram_id, language, role, status, notifications_enabled, created_at, updated_at`) и строку в `group_admins`; `upgrade head`; ожидается ровно три назначения: админ (Админ, `group_id` NULL), ДСВР (ДСВР, NULL), админ группы (Админ группы, id группы `music`).
  - Существующий `test_migrations_match_the_models` должен пройти без изменений.
- [ ] **Step 2:** `pytest tests/test_roles_schema.py -v`. Ожидается FAIL (нет модуля каталога и таблиц).
- [ ] **Step 3: Каталог и модели** как в Interfaces. Данные каталога брать из Global Constraints дословно.
- [ ] **Step 4: Ревизия `0004_roles.py`** (`down_revision = "0003_schedule_change_booking_date"`): создаёт три таблицы (по образцу `0003`: пропускать то, что уже есть в базе, принятой из `create_all`), вставляет три роли и права литералами (без импорта каталога), переносит `users.role in ('admin','dsvr')` и строки `group_admins` в `role_assignments`. `downgrade` удаляет три таблицы.
- [ ] **Step 5:** `pytest tests/test_roles_schema.py tests/test_migrations.py -v`. Ожидается PASS.
- [ ] **Step 6: Commit** `Add the role tables, the permission catalog and the migration that moves today's staff into them`.

---

### Task 2: Проверки прав с мостом

**Files:**
- Modify: `app/services/permissions.py`, `tests/conftest.py`
- Create: `app/services/roles.py` (только сидинг), `tests/test_permission_checks.py`

**Interfaces:**
- Consumes: Задача 1.
- Produces (`roles.py`): `async ensure_admin_role(session) -> Role` (создаёт «Админ», если нет; без commit), `async seed_default_roles(session) -> None` (создаёт все три роли, если таблица `roles` пуста; без commit).
- Produces (`permissions.py`):
  - `class Grant(NamedTuple): permission: str; group_id: int | None`
  - `async user_grants(session, user) -> list[Grant]` (пустой для неодобренного; роль с `is_system` даёт `Grant(p, None)` на весь каталог; групповое назначение даёт права только групповой области; мост добавляет права по `User.role` и `GroupAdmin` из `DEFAULT_ROLES`)
  - `async has_permission(session, user, perm: str, group_id: int | None = None) -> bool`
  - `async require_permission(session, user, perm, group_id=None) -> None` (`ForbiddenError("Admin access required")`)
  - `async permitted_group_ids(session, user, perm) -> set[int] | None` (`None` = все группы; для права «club» без клубного назначения пустое множество)
  - `async holds_admin_role(session, user) -> bool`, `async has_any_role(session, user) -> bool`, `async has_permission_anywhere(session, user, perm) -> bool`
  - `async holders_of(session, perm: str, group_id: int | None = None) -> list[User]` (одобренные держатели: клубные и, если задана группа, групповые)
- Produces (`conftest.py`): фикстура `session` вызывает `roles.seed_default_roles`; фикстура `grant` — `async grant(user, role_name: str, group_slug: str | None = None) -> RoleAssignment`.

- [ ] **Step 1: Тесты `tests/test_permission_checks.py`:**
  - `test_admin_role_has_every_permission_everywhere`: для всех ключей `PERMISSIONS` и для группы/без группы `has_permission` True.
  - `test_default_roles_match_legacy`: таблица «кто что может сегодня»: ДСВР клубно: `bookings.priority`, `bookings.manage`, `schedule.history` True, `users.moderate`, `settings.manage`, `audit.view`, `events.manage`, `roles.manage` False; админ группы `music`: `users.moderate`/`bands.manage`/`bookings.manage`/`schedule.history` True для `music`, False для `dance` и для `group_id=None`, остальное False.
  - `test_club_only_permission_ignores_group_scoped_assignment` (Review Focus 1): роль с `audit.view`, выданная на группу, даёт `has_permission(..., "audit.view") == False`.
  - `test_unapproved_holder_has_nothing` (Review Focus 2): для статусов `pending/rejected/banned/deleted` при назначении «Админ» всё False и `permitted_group_ids` пуст.
  - `test_permitted_group_ids`: клубное назначение → `None`; групповое → `{id_music}`; без назначения → `set()`.
  - `test_legacy_columns_still_grant`: `make_user(role="admin")` → `holds_admin_role`; строка `GroupAdmin` → `users.moderate` на свою группу.
  - `test_holders_of_lists_club_and_group_holders_and_skips_unapproved`.
  - `test_require_permission_raises_forbidden` (сообщение «Admin access required»).
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Реализовать** `roles.py` (сидинг), функции `permissions.py` по Interfaces и правки `conftest.py`. Старые функции `permissions.py` оставить как есть.
- [ ] **Step 4:** `pytest -q` целиком. Ожидается PASS, предупреждений нет.
- [ ] **Step 5: Commit** `Check permissions through roles, bridging the legacy role columns`.

---

### Task 3: Сервис ролей

**Files:**
- Modify: `app/services/roles.py`, `app/services/audit.py` (`AUDIT_CATEGORIES`), `app/services/errors.py`, `app/db/session.py` (`seed_reference_data` вызывает `ensure_admin_role`), `app/locales/{ru,en,kz}.json`
- Test: `tests/test_roles_service.py`

**Interfaces:**
- Consumes: Задача 2.
- Produces (`roles.py`), первым аргументом `session`, вторым `actor_telegram_id: int`:
  - `async list_roles(session, actor_telegram_id) -> list[dict]` (`{"id","name","is_system","permissions": list[str],"holders": int}`; «Админ» показывает весь каталог)
  - `async create_role(session, actor_telegram_id, name: str, permissions: list[str]) -> Role`
  - `async update_role(session, actor_telegram_id, role_id: int, name: str | None, permissions: list[str] | None) -> Role`
  - `async delete_role(session, actor_telegram_id, role_id: int) -> int` (число снятых назначений)
  - `async list_assignments(session, actor_telegram_id, role_id: int) -> list[RoleAssignment]`
  - `async assign_role(session, actor_telegram_id, target_identifier: str, role_id: int, group_id: int | None) -> RoleAssignment`
  - `async revoke_assignment(session, actor_telegram_id, assignment_id: int) -> User`
  - Все требуют `roles.manage` (`require_permission`); работа с ролью «Админ» (назначить / снять) требует `holds_admin_role(actor)`.

- [ ] **Step 1: Тесты `tests/test_roles_service.py`:**
  - `test_create_role_stores_permissions_and_logs`: запись `role.create` в `AuditLog`.
  - `test_create_role_rejects_bad_names_and_keys` (Review Focus 4): пустое, из пробелов, 61 символ, дубликат в другом регистре → `ConflictError`; неизвестный ключ → `ConflictError`.
  - `test_only_roles_manage_holders_may_use_the_service`: обычный пользователь и держатель `users.moderate` → `ForbiddenError`.
  - `test_update_role_renames_and_replaces_permissions`; `test_system_role_cannot_be_changed_or_deleted`.
  - `test_delete_role_removes_assignments`: возвращает число назначений; `test_actor_may_delete_a_role_they_hold` (Review Focus 5): после удаления `has_permission` актора False.
  - `test_assign_role_club_and_group`; `test_duplicate_assignment_is_refused` (Review Focus 3, оба варианта: NULL и группа); `test_assign_requires_approved_target`; `test_assign_unknown_group_is_not_found`.
  - `test_admin_role_is_club_wide_and_only_admin_holders_assign_it`: назначение «Админ» с группой → `ConflictError`; держатель `roles.manage` без «Админ» → `ForbiddenError`.
  - `test_revoke_guards`: нельзя снять «Админ» с последнего держателя, с пользователя из `INITIAL_ADMIN_TELEGRAM_IDS` (monkeypatch `settings.initial_admin_ids`) и с себя.
  - `test_assign_and_revoke_notify_target`: событие в `published`, если `notifications_enabled`; не отправляется, если выключены; журнал `role.assign` / `role.revoke`.
  - `test_list_roles_counts_holders`.
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Реализовать** сервис, добавить `"role"` в `AUDIT_CATEGORIES`, сообщения ошибок и по ключу `webapp.error.code.*` в трёх локалях, вызвать `ensure_admin_role` в `seed_reference_data`. Образец стиля — `services/groups.py` и `users.py`.
- [ ] **Step 4:** `pytest -q`. Ожидается PASS (включая `test_error_codes`).
- [ ] **Step 5: Commit** `Add the role service: create, edit, delete, assign and revoke`.

---

### Task 4: Модерация, коллективы, получатели заявок

**Files:**
- Modify: `app/services/users.py`, `app/services/permissions.py` (`can_manage_band`, `can_view_user`), `app/services/bands.py` (`list_all_bands`), `app/services/groups.py` (`registration_reviewer_ids`), `app/services/move_requests.py:59`
- Test: `tests/test_custom_role_moderation.py`

**Interfaces:**
- Consumes: `has_permission`, `permitted_group_ids`, `holds_admin_role`, `has_any_role`, `holders_of` (Задача 2).
- Produces: `_guard_banned_or_deleted`, `_require_moderation_rights` в `users.py` становятся `async` и принимают `session`; `require_approved(user)` проверяет только `status`.

- [ ] **Step 1: Тесты `tests/test_custom_role_moderation.py`** (роль создаётся через `roles.create_role` и выдаётся `grant`):
  - `test_custom_role_moderates_only_its_group`: роль `users.moderate` на группу `dance` одобряет участника `dance` и получает `ForbiddenError` на участнике `music`.
  - `test_moderator_cannot_touch_people_with_roles`: цель с любым назначением модерируется только держателем «Админ».
  - `test_only_admin_role_restores_banned_users`.
  - `test_scoped_user_list_follows_permission`: `search_users` для клубного права видит всех, для группового только свою группу, без права `ForbiddenError`.
  - `test_bands_manage_scopes_list_all_bands_and_can_manage_band`.
  - `test_registration_reviewers_come_from_roles`: `registration_reviewer_ids(session, group_id)` содержит клубных держателей `users.moderate` и групповых этой группы.
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Перевести** места из File list: права `users.moderate` (на `target.group_id`) и `bands.manage` (на `band.group_id`). Цель с ролями (`has_any_role`) требует `holds_admin_role(admin)`. Админский обход в `require_approved` и в `move_requests.py:59` убрать. `annotate_group_admin` не трогать.
- [ ] **Step 4:** `pytest -q`. Ожидается PASS без изменений существующих тестов.
- [ ] **Step 5: Commit** `Decide moderation and band access from permissions`.

---

### Task 5: Брони, история, планирование

**Files:**
- Modify: `app/services/bookings.py` (`create_staff_booking`, `_require_priority_rights`, `by_admin`, привилегированный путь в `update_booking`), `app/services/permissions.py` (`can_manage_schedule`), `app/services/schedule_history.py` (`list_changes`), `app/services/planning.py` (запуск распределения)
- Test: `tests/test_custom_role_bookings.py`

**Interfaces:**
- Consumes: Задача 2.
- Produces: `_require_priority_rights(session, user)` становится `async` (`bookings.priority`); текст вытеснения: «админом», если `holds_admin_role`, иначе «ДСВР».

- [ ] **Step 1: Тесты:**
  - `test_bookings_manage_club_wide_edits_any_booking_and_scoped_only_its_group`: клубная роль правит бронь коллектива любой группы и личную; групповая только брони своей группы.
  - `test_priority_permission_takes_the_room_and_bypasses_limits`: кастомная роль с `bookings.priority` вызывает `create_priority_booking` и переносит бронь выше недельного лимита; без права `ForbiddenError`.
  - `test_schedule_history_scope`: `schedule.history` клубное видит всё, групповое только свою группу, без права только историю своего коллектива (как сегодня).
  - `test_settings_manage_runs_allocation`: актор с `settings.manage` запускает распределение, без права `ForbiddenError` (имя функции — точка входа в `planning.py` с докстрингом «actor_telegram_id None means the scheduler»).
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Перевести** места: `bookings.manage` на `band.group_id` (брони без коллектива требуют клубного права), `bookings.priority`, `schedule.history`, `settings.manage`.
- [ ] **Step 4:** `pytest -q`. Ожидается PASS, включая `test_dsvr.py`, `test_schedule_history.py`.
- [ ] **Step 5: Commit** `Decide booking, history and planning access from permissions`.

---

### Task 6: Настройки, журнал, концерты, охрана роутов админки

**Files:**
- Modify: `app/services/admin.py` (13 вызовов `require_admin`), `app/services/audit.py:61`, `app/services/events.py` (4 вызова), `app/api/routes/webapp_admin.py`
- Test: `tests/test_custom_role_settings.py`, дополнить `tests/test_webapp_admin.py`

**Interfaces:**
- Consumes: Задача 2.
- Produces (`webapp_admin.py`): зависимости `reader_with(*perms: str)` и `writer_with(*perms: str)` (читают `current_webapp_user` / `fresh_webapp_user`; допускают пользователя, у которого есть любое из прав в любой области; иначе `ForbiddenError`).
- Соответствие: `/admin/users*` → `users.moderate`; `/admin/settings*` → `settings.manage`; `/admin/history` → `schedule.history`; `/admin/actions` → `audit.view`. Роуты `/admin/staff*` и `/admin/groups/{id}/admins` пока остаются на `user_service.require_admin` (удаляются в Задаче 10). Список групп в `/admin/history`: все при `permitted_group_ids(...) is None`, иначе только разрешённые.

- [ ] **Step 1: Тесты:**
  - `test_settings_manage_holder_updates_settings_others_refused`, `test_audit_view_opens_journal`, `test_events_manage_creates_concert` (сервисный уровень).
  - Через HTTP (по образцу `tests/test_webapp_admin.py`): `test_custom_role_opens_only_its_endpoints`: пользователь с `settings.manage` получает 200 на `GET /admin/settings` и 403 на `GET /admin/users`; с `schedule.history` видит `/admin/history`, но не `/admin/actions`.
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Перевести:** `settings.manage` (admin.py, включая `get_personal_bookings_enabled` с `require_admin_access`), `audit.view`, `events.manage`; роуты по соответствию выше.
- [ ] **Step 4:** `pytest -q`. Ожидается PASS.
- [ ] **Step 5: Commit** `Guard the admin API and the club-wide services by permissions`.

---

### Task 7: API ролей, `/me`, карточки людей

**Files:**
- Modify: `app/api/routes/webapp_admin.py`, `app/api/routes/webapp.py` (`/me`), `app/schemas/webapp_admin.py`, `app/locales/{ru,en,kz}.json`
- Test: `tests/test_webapp_roles_api.py`

**Interfaces:**
- Consumes: сервис ролей (Задача 3), `reader_with` / `writer_with` (Задача 6).
- Produces (`schemas/webapp_admin.py`): `WebAppRoleCreate(name: str, permissions: list[str])`, `WebAppRoleUpdate(name: str | None, permissions: list[str] | None)`, `WebAppRoleAssign(target: str, role_id: int, group_id: int | None)`.
- Produces (роуты, все `roles.manage`, префикс `/webapp/api/admin`):
  - `GET /roles` → `{"roles": [...как list_roles], "catalog": [{"key","scope"}]}`
  - `POST /roles`, `PATCH /roles/{id}`, `DELETE /roles/{id}` (ответ `{"removed_assignments": int}`)
  - `GET /roles/{id}/assignments` → `[{"id","role_id","person": person(), "group": {...}|null}]`
  - `POST /role-assignments`, `DELETE /role-assignments/{id}`
- Produces (`/me`): добавляются `roles: [{"name","group": {...}|null}]` и `permissions: {key: {"all": bool, "group_ids": [int]}}`; прежние `is_admin`, `is_dsvr`, `administered_groups` остаются до Задачи 11. `person()` получает `roles: [{"assignment_id","name","group"}]`, поле `role` остаётся до Задачи 11.
- Locale-ключи: `webapp.admin.perm.<ключ каталога>` (9 подписей) и `webapp.admin.perm.club_only` (подсказка «только на весь клуб»).

- [ ] **Step 1: Тесты:** создание роли и появление её в списке; `PATCH` и `DELETE` (число снятых назначений); назначение и снятие через HTTP; 403 без `roles.manage`; 422 или 409 на неизвестный ключ права; `/me` отдаёт `permissions` в формате выше для клубного и группового назначения; `person()` содержит `roles`.
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Реализовать** роуты, схемы, расширение `/me` и `person()`, ключи локалей ru/en/kz.
- [ ] **Step 4:** `pytest -q`. Ожидается PASS.
- [ ] **Step 5: Commit** `Expose roles and permissions through the Mini App API`.

---

### Task 8: Mini App

**Files:**
- Modify: `app/webapp/static/app.js`, `app/locales/{ru,en,kz}.json`

**Interfaces:**
- Consumes: API Задачи 7 (`/admin/roles*`, `/admin/role-assignments`, `/me.permissions`, `person.roles`).
- Produces (`app.js`): `can(perm, groupId = null) -> boolean` над `state.me.permissions`.

- [ ] **Step 1: Перевод проверок на `can()`:** `app.js:485` (режим занять комнату) → `can("bookings.priority")`; `ADMIN_TABS` / `MAIN_ADMIN_TABS` (около 1402–1437): заявки и пользователи → `users.moderate`, история → `schedule.history`, настройки → `settings.manage`, журнал → `audit.view`, роли → `roles.manage`; бейджи ролей (около 1494–1506 и 2024–2026) строятся из `person.roles` / `me.roles`.
- [ ] **Step 2: Вкладка «Роли»** (замена `roleSections` и вызова `/admin/staff`, около 1613–1620 и 1800): список ролей с числом держателей; редактор (название, галочки по `catalog`, права «весь клуб» с подсказкой `webapp.admin.perm.club_only`); лист «Назначить» (поле пользователя, выбор роли, область «весь клуб» или группа); список держателей с «Снять»; удаление роли с подтверждением «роль выдана N людям».
- [ ] **Step 3: Локали:** недостающие ключи `webapp.admin.roles.*` в ru/en/kz (одинаковый набор ключей); удалить ключи, на которые больше нет ссылок (`webapp.role.*`, `webapp.admin.role.*`, `webapp.admin.roles.add_group_admin` и др.), проверив `grep` по `app.js` перед удалением.
- [ ] **Step 4: Проверка:** `node --check app/webapp/static/app.js` (если Node установлен), `pytest -q` (проверка локалей), затем запустить приложение (skill `run`; dev-overlay compose) и пройти руками: создать роль «Модератор» с `users.moderate` на группу Денс; назначить; войти этим пользователем и увидеть только вкладки «Заявки» и «Пользователи» и только участников Денс; удалить роль с подтверждением.
- [ ] **Step 5: Commit** `Build the role constructor in the Mini App and drive tabs by permission`.

---

### Task 9: Бот

**Files:**
- Modify: `app/schemas/users.py`, `app/services/users.py` (`annotate_group_admin` → `annotate_access`), `app/bot/handlers.py` (около 108, 115, 495, 627, 635, 1238, 1477), `app/bot/handlers_planning.py:297`, `app/bot/keyboards.py` (`staff_inline_keyboard`)
- Test: существующие бот-тесты, при необходимости дополнить

**Interfaces:**
- Produces: `UserRead.is_admin: bool = False` и `UserRead.permissions: list[str] = []` (ключи с любой областью); `async annotate_access(session, user) -> User` ставит `user.is_admin`, `user.permissions`. Поля `role` и `is_group_admin` остаются до Задачи 11.
- Produces: `staff_inline_keyboard(permissions: list[str], is_admin: bool, webapp_base_url: str | None = None)`; меню персонала видно, если у пользователя есть хоть одно право; пункты модерации по `users.moderate`, «занять комнату» по `bookings.priority`; планирование (`handlers_planning._require_admin`) по `settings.manage`; `main_menu_inline(is_admin)` остаётся по `is_admin`.

- [ ] **Step 1: Тест** в `tests/test_access_control.py`: `annotate_access` для админа, ДСВР и админа группы возвращает `is_admin` / `permissions`, как в таблице Задачи 2; `UserRead` содержит новые поля.
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Реализовать** схему, `annotate_access` (заменить все вызовы `annotate_group_admin`), перевести места бота.
- [ ] **Step 4:** `pytest -q`; `python -m compileall -q app`. Ожидается PASS.
- [ ] **Step 5: Commit** `Drive the bot menus by permissions instead of role names`.

---

### Task 10: Начальные админы, бан и удаление на назначениях; удаление старого кода

**Files:**
- Modify: `app/services/roles.py`, `app/services/users.py`, `app/db/session.py`, `app/api/routes/webapp_admin.py`, `app/services/groups.py`
- Delete из кода и тестов: `promote_to_admin`, `demote_admin`, `set_dsvr_role`, `list_admins`, `require_admin`, `add_group_admin`, `remove_group_admin`, `list_group_admins`, роуты `/admin/staff*`, `/admin/groups/{id}/admins`, схема `WebAppRoleChange`, и тесты именно этих функций (их покрывают тесты Задач 3 и 7)

**Interfaces:**
- Produces (`roles.py`): `async ensure_initial_admin(session, user) -> None` (создаёт клубное назначение «Админ», если нет; без commit), `async revoke_all(session, user) -> None` (снимает все назначения; без commit).

- [ ] **Step 1: Тесты** (в `tests/test_roles_service.py` и существующих файлах по `/start` и бану): `upsert_telegram_user` для id из `INITIAL_ADMIN_TELEGRAM_IDS` даёт `holds_admin_role`; `seed_reference_data`-путь (`ensure_initial_admin` на существующем пользователе) то же; `ban_user` и `soft_delete_user` снимают все назначения цели, и она теряет права.
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Реализовать** `ensure_initial_admin` (вызывать из `upsert_telegram_user` и `seed_reference_data` вместо `UPDATE users SET role='admin'`) и `revoke_all` (из `ban_user`, `soft_delete_user`, сброс удалённого пользователя при повторной регистрации); удалить перечисленное. Запись `target.role = ...` в `users.py` пока не трогать (колонка живёт до Задачи 12).
- [ ] **Step 4:** `pytest -q`; `grep -rn "promote_to_admin\|demote_admin\|set_dsvr_role\|list_admins\|add_group_admin" app tests` пусто.
- [ ] **Step 5: Commit** `Make role assignments the source of truth for admins, bans and deletions`.

---

### Task 11: Фикстуры на назначениях, удаление моста

**Files:**
- Modify: `tests/conftest.py` и все тесты, где есть `role=`, `.role`, `GroupAdmin` (`grep -rln "UserRole\|GroupAdmin\|role=\|\.role\b" tests`); `app/services/permissions.py`; `app/api/routes/webapp.py`; `app/api/routes/webapp_admin.py`; `app/schemas/users.py`; `app/services/users.py`

**Interfaces:**
- Produces (`conftest.py`): `make_user(role="user"|"admin"|"dsvr", ...)` сохраняет сигнатуру, но «admin» и «dsvr» выдают назначение стартовой роли, а `User.role` не пишется; `async make_group_admin(user, group_slug) -> RoleAssignment` (роль «Админ группы» на группу) вместо прямых вставок `GroupAdmin`.

- [ ] **Step 1:** Переписать `make_user` и добавить `make_group_admin`; заменить `GroupAdmin(...)` в тестах на `make_group_admin`; ассерты вида `user.role == "admin"` заменить на `holds_admin_role`.
- [ ] **Step 2:** `pytest -q`. Ожидаются падения только там, где мост был нужен; чинить тесты, не код.
- [ ] **Step 3: Удалить мост** (`_legacy_grants` и его вызов в `user_grants`), старые `is_global_admin`, `is_dsvr`, `is_group_admin`, `require_group_admin`, `administered_group_ids` из `permissions.py`; из `/me` убрать `is_admin`, `is_dsvr`, `administered_groups`, из `person()` поле `role`, из `UserRead` поля `role`, `is_group_admin`; `annotate_access` оставить.
- [ ] **Step 4:** `pytest -q` PASS; `grep -rn "is_global_admin\|is_dsvr\|is_group_admin\|administered_group_ids\|annotate_group_admin" app tests` пусто.
- [ ] **Step 5: Commit** `Drop the legacy role bridge and move the tests onto role assignments`.

---

### Task 12: Удаление старой схемы и документация

**Files:**
- Modify: `app/db/models.py` (убрать `UserRole`, `User.role`, `GroupAdmin`; `GROUP_SLUGS` остаётся), `app/services/users.py` и остальные упоминания `UserRole`, `README.md`, `CLAUDE.md`, `docs/backlog.md`
- Create: `alembic/versions/0005_drop_legacy_roles.py`
- Test: `tests/test_roles_schema.py`

**Interfaces:**
- Consumes: Задача 1 (назначения уже содержат перенесённых людей).

- [ ] **Step 1: Тест** `test_legacy_role_column_and_group_admins_are_dropped`: после `upgrade head` в `users` нет колонки `role`, нет таблицы `group_admins`, `downgrade` на `0004_roles` возвращает обе (колонка с `default 'user'`).
- [ ] **Step 2:** запустить, ожидается FAIL.
- [ ] **Step 3: Ревизия `0005_drop_legacy_roles.py`** (`down_revision = "0004_roles"`, `batch_alter_table` для SQLite), правки моделей, удаление всех оставшихся `UserRole` / `role=` в коде (`grep -rn "UserRole\|GroupAdmin" app` пусто).
- [ ] **Step 4: Документация:** README (роли, каталог прав, как создать роль), CLAUDE.md (раздел Permissions и упоминания admin/DSVR/group admin в Architecture заменить на роли и `has_permission`; убрать `administered_group_ids` / `GroupAdmin`), backlog (отметить выполненное; добавить «Управление группами из интерфейса» как следующую задачу).
- [ ] **Step 5: Итоговая проверка:** `pytest -q` PASS без warnings; `python -m compileall -q app`; `alembic check` без дрейфа; `test_migrations.py` PASS.
- [ ] **Step 6: Commit** `Remove the legacy role column and group_admins table, document the roles`.
