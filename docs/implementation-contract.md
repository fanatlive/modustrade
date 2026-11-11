# Контракт реализации MVP

Текущий контракт реализации: Django 5.2 LTS, PostgreSQL 17, серверные HTML-шаблоны.
SQLite допустим только для локального демо при DEBUG=1 и не подтверждает
конкурентность или производительность PostgreSQL. Код приложения — feedback;
имена URL используют namespace feedback.

## Связанные контракты

- [Матрица команд](command-matrix.md): состояния, типы, полномочия, обязательные поля и краевые случаи.
- [HTTP-интерфейс](http-interface.md): пути, методы, коды ошибок и фильтры.
- [Поиск](search.md): generated document, GIN и совместимость SQLite.
- [Импорт](import.md): стабильные идентификаторы, staging, dry-run и сохранение происхождения.
- [Эксплуатация](operations.md): production, TLS/proxy, роли БД, heartbeat, метрики и восстановление.

Исполняемые источники: feedback/models.py, feedback/services.py,
feedback/permissions.py и feedback/contracts.py. Ни HTML-кнопки, ни этот документ
не заменяют серверную проверку бизнес-команд.

## Модель и значения
User(AbstractUser): role (author, processor, coordinator, manager, admin), extra_roles JSON list, org_unit FK nullable, restricted_access bool. role_label property. Admin приложения не получает права чтения автоматически. superuser только аварийный административный доступ.
OrgUnit: name, code unique.
Category: name, code unique, active.
Queue: name, code unique, restricted bool, owner FK User nullable, members M2M User.
RoutingRule: category FK nullable, request_type blank (PROBLEM/SUGGESTION/PRAISE), affected_unit FK nullable, queue FK, priority int, active, version int, supersedes OneToOne self nullable. Условия и номер принятой версии неизменяемы; изменение создаёт следующую версию, прежняя деактивируется и сохраняется для истории.
Ticket: UUID PK; number unique string — для новых пользовательских карточек MT- и 28 hex-символов UUID, для исходных демо-карточек сохраняются MT-A-001…MT-A-020; author FK; category FK; affected_unit FK nullable; author_unit_name snapshot; queue FK; assignee FK nullable; request_type; description; proposed_solution; search_document generated; reported_priority/effective_priority LOW/MEDIUM/HIGH; confidentiality STANDARD/RESTRICTED; status NEW/REVIEW/WAITING_INFO/IN_PROGRESS/CLOSED; decision blank/APPROVED/DECLINED/ACKNOWLEDGED; criteria; plan_due_at nullable; resolution_code blank/IMPLEMENTED/DECLINED/ACKNOWLEDGED/DUPLICATE/WITHDRAWN/NO_RESPONSE; resolution_summary; closed_at nullable; created_at default timezone.now; submitted_at nullable; updated_at; version default1; cycle_no default1; is_demo; source_record_id nullable unique; source_data JSON; quality_flags JSON list. title property (первая строка с ограничением длины), status_label/type_label/priority_label, current_cycle property с использованием prefetch, is_overdue property.
search_document — persisted GeneratedField с output_field SearchVectorField. PostgreSQL автоматически вычисляет russian to_tsvector по description и proposed_solution; поле индексируется GIN. На SQLite хранится пустая строка, поиск использует icontains. Generated fields исключены из явного UPDATE бизнес-команды; обычные выборки откладывают получение вектора. Миграция 0008 удаляет прежний функциональный GIN; индексы номеров/source_record_id остаются. Подробнее — [поиск](search.md).
ProcessingCycle: ticket FK related_name cycles, number; started_at nullable; first_response_due_at/decision_due_at/first_response_at/decision_at/closed_at nullable; resolution_code, resolution_summary; policy JSON. unique ticket+number.
Decision: ticket FK related_name decisions; actor FK; code; reason; criteria; created_at; cycle_no.
InfoRequest: ticket FK related_name info_requests; actor FK; question; due_at; resume_status; response blank; answered_at nullable; expired_notified bool; reminder_generation default1; reminder_due_at nullable; reminder_delivered_at nullable; pause_until nullable; paused_seconds int; created_at. Не более одного активного вопроса на карточку. reminder_due_at хранит рассчитанное по календарю время постановки напоминания и очищается после атомарного enqueue; продление создаёт следующее поколение напоминания. reminder_delivered_at отражает фактическое создание уведомления автору.
Comment: ticket FK related_name comments; author FK; message; visibility public/internal; created_at.
Event: ticket FK related_name events; actor FK nullable; kind; summary; visibility public/internal; payload JSON; sequence int; origin (runtime/import); created_at. unique ticket+sequence. Новые runtime-события сохраняют в payload.state_snapshot состояние, решение, классификацию, назначение, сроки и флаги ранее нарушенного SLA для исторической аналитики. Синтетические дополнения импорта отмечаются отдельно; отсутствие исходной даты не заменяется датой импорта.
Outbox: event FK; recipient FK; ticket FK; message; state pending/processing/sent/failed; attempts; next_attempt_at; locked_until nullable; last_error. unique event+recipient.
Notification: user FK related_name notifications; ticket FK; outbox OneToOne; message; created_at; read_at nullable.
IdempotencyRecord: actor FK; key; payload_hash; ticket FK; created_at. unique actor+key.
ImportBatch: checksum unique; source_name; created_at; summary JSON.
ImportRecord: batch FK; source_id; raw JSON; issues JSON; ticket FK nullable; unique batch+source_id.
TicketRelation: ticket FK related_name duplicate_relations, canonical FK; active bool; archived_at nullable. Уникальна только активная связь по ticket (partial unique constraint); архивные связи сохраняются. Запрещены самоссылки и циклы активного графа. Повторное открытие архивирует текущую связь; сервис сериализует изменения графа.
RateLimitBucket: key unique (тип лимита и хэш идентификатора), count, expires_at; общие для процессов счётчики обоих входов и пользовательских мутаций.
WorkerHeartbeat: key unique (default), last_seen, last_error, last_counts JSON; отражает последний проход фонового обработчика.

## Сервисный API
DomainError(message, code='validation', status=422); ConflictError status409. Raise django.core.exceptions.PermissionDenied for denied scope.
visible_tickets(user) QuerySet; visible_notifications(user) QuerySet; can_process(user,ticket) bool; can_coordinate(user,ticket=None) bool; can_view_internal(user,ticket) bool; has_role(user,*roles) bool in permissions.py. Проверяются активность, допуск, роль и область очереди/назначения. Автор видит собственное обращение; авторство само по себе не даёт права обработки. Автор не получает внутренние события даже при дополнительной операционной роли. Отозванные роли/допуски повторно проверяются при чтении уведомлений. Некорректная структура extra_roles не расширяет права; User.clean проверяет список допустимых кодов.
create_ticket(*, actor, data:dict, idempotency_key:str) -> Ticket. data: category (object or id), request_type, description, proposed_solution, reported_priority, confidentiality, affected_unit optional. atomic idempotency+event+outbox.
execute_command(*, actor, ticket_id, command:CommandName | str, expected_version:int, data:CommandPayload | dict) -> Ticket. Команды: start, assign, approve, decline, acknowledge, ask_info, answer_info, resume_info, extend_info, reclassify, implement, reopen, withdraw, duplicate, no_response, comment. DTO описаны в feedback/contracts.py; поля включают message/visibility, reason, criteria, plan_due_at, question/due_at, assignee_id/queue_id, category_id/effective_priority/request_type, result/evidence, canonical_id. Команда проверяет доступ, состояние, тип и версию; одна транзакция сохраняет изменение, одно событие команды и outbox. Содержательные публичные комментарии/вопросы/решения считаются первым ответом; внутренние сообщения — нет. После CLOSED разрешён только reopen. Полные условия — [матрица команд](command-matrix.md).
eligible_assignee(user,queue,restricted) проверяет активность, операционную роль, членство/владение очередью и конфиденциальный допуск. Переход в IN_PROGRESS по одобрению требует подходящего исполнителя. Повторное открытие может сохранить новый REVIEW без назначения в прежней очереди; старый цикл не меняется. Ответ автора на уточнение сохраняется независимо от доступности исполнителя. При невозможности продолжить одобренную реализацию используется REVIEW с cycle.policy.assignment_resume_status=IN_PROGRESS; решение и план сохраняются, явный assign возобновляет работу, просрочка продолжает учитываться.
retry_delivery(*,actor,outbox_id,reason) повторно ставит failed-доставку в той же идентичности с внутренним аудитом; область координатора и внутренний доступ проверяются. reconcile_assignments(*,actor,apply=False,limit=100) по умолчанию возвращает план передачи недействительных назначений, применение использует обычный assign. revise_routing_rule и disable_routing_rule управляют версиями маршрутов с административным аудитом.
process_outbox(limit=100) -> dict sent/failed/skipped. Проход включает индексируемый выбор напоминаний, истечение уточнений, контроль SLA и доставку. Используются lease и PostgreSQL skip_locked; до 8 неудачных попыток с backoff, затем failed и явный повтор координатором. Перед фактической доставкой заново проверяются доступ и поколение напоминания. Notification уникален по outbox; отозванный доступ завершает задачу без пользовательского уведомления. Management command process_outbox: --once (один проход), --loop, --limit, --interval; обновляет WorkerHeartbeat.
operations_snapshot() в feedback/operations.py возвращает безопасные агрегаты очереди, heartbeat и alerts. Возраст недоставленного события oldest_pending_seconds не обнуляется backoff; ready_job_lag_seconds отдельно показывает просрочку следующей попытки. HTTP-метрики требуют OPERATIONS_TOKEN. Настройки и команды — [эксплуатация](operations.md).
sla.py: add_business_hours(dt,hours,policy=...), subtract_business_hours, business_seconds(start,end,policy=...), reopen_deadline, initial_deadlines, policy_snapshot. Календарь сохраняется с циклом и используется для последующих расчётов, включая паузы и окно reopen. Демо: Asia/Bishkek, Mon–Fri 09–13/14–18, без праздников; первоначальные сроки HIGH 4/24, MEDIUM 8/80, LOW 16/160 рабочих часов. Пауза решения при ожидании автора — суммарно до 40 часов, первый ответ не приостанавливается. NO_RESPONSE требует доставленного напоминания и ещё 8 рабочих часов после доставки, в том числе после позднего запуска worker.

## UI URLs и страницы
login /accounts/login/; logout POST /accounts/logout/. feedback:list path /; detail /requests/<uuid:pk>/; create /requests/new/; command /requests/<uuid:pk>/command/ POST (command,version); queue /queue/; analytics /analytics/; settings /settings/; notifications /notifications/; read_notification POST. Поиск canonical и similar ограничен доступными карточками; failed/retry доставки доступны координатору своей области. /health/, /health/worker/ и авторизованный /operations/metrics/ описаны в [HTTP-контракте](http-interface.md).
Use LoginView built-in, forms csrf, server errors409/422 rendered clear. No external CDN/fonts/network. SVG inline allowed. Mobile sidebar collapses through accessible menu. Badge demo/review warnings visible.
Design: sidebar navy#173A46, accent teal#117C79, background#F2F6F7, text#172F3A, muted#657782, white. System fonts Segoe UI/Arial (Cyrillic), tabular numbers. Less boxed clutter, clean table, strong detail timeline and clear action panel. All UI Russian, no infrastructure jargon in product pages.

## Данные
data/demo_requests.json содержит извлечённые двадцать синтетических записей с исходными source_id A-001..A-020 и контрольной суммой источника. ImportRecord сохраняет raw, а новая противоречащая версия не перезаписывает рабочую карточку. Категории сопоставляются по стабильному коду/нормализованному имени с сохранением legacy PK; неоднозначность отклоняется. Демо-пользователи author/processor/coordinator/manager/admin, author2 и остальные исходные роли — синтетические учётные записи; роли источника не считаются реальными личностями. DEMO_PASSWORD относится только к новым аккаунтам. Повтор не сбрасывает пароли, статусы, историю и настройки.
Демо-bootstrap создаёт резервные/конфиденциальную очереди, состав и маршруты;
A-017 (корпоративное предложение) направляется в admin, A-018 (положительный
отзыв) — в hr. Для исходных снимков submitted_at=None. A-004/A-018 переводятся
в REVIEW с поясняющими quality_flags и сохранением исходного статуса.
A-005/A-013 имеют явно синтетические вопросы и сроки; A-008 — явно синтетический
учебный результат и тестовый артефакт, не подтверждение работы компании.
Сгенерированные даты и дополнения отмечаются в source_data. Демо исключено из
production KPI; примеры желаемых действий источника не считаются выполненными
действиями. Файловый модуль в MVP отсутствует, фиктивного upload в UI нет.

Сценарные дополнения одобрения/результата/вопроса относятся только к точной
доверенной исходной строке. Изменённая строка свежего набора сохраняется как
REVIEW с quality_flags без переноса прежнего сценарного результата. Несовместимое
сочетание PRAISE и исходных «В работе»/«Реализовано»/«Отклонено» получает
SOURCE_SEMANTIC_CONFLICT; raw сохраняется. Новая версия уже импортированной
карточки остаётся в staging, не изменяя рабочее состояние.

load_demo поддерживает --dataset, --bootstrap-routes, --dry-run, --json и --report.
Автоматический bootstrap демо-настроек разрешён на пустой установке; последующие
партии не восстанавливают изменённые маршруты/состав очередей. Отдельный
bootstrap_initial --coordinator LOGIN [--dry-run] настраивает резервные очереди
с явно назначенным действующим координатором и без демо-данных/паролей.
Производственные показатели исключают is_demo=True; когорта определяется по
submitted_at, as_of не может быть будущим. Для legacy-данных без полных снимков
аналитика явно сообщает ограничения восстановления истории.
