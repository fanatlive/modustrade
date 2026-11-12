# Требования, реализация и регрессии

Область — работающий MVP из [спецификации](specification.md), а не все возможные
продукты, упомянутые в синтетических обращениях. Указание файла теста означает
постоянную регрессию; итог конкретного запуска находится в [протоколе](verification.md).

| Требование / исходный дефект | Реализация | Проверка |
| --- | --- | --- |
| Изоляция автора, очередей и допуска; F02 | permissions.visible_tickets/can_view_internal | test_ui_remediation: authorship_overrides_additional_operational_role; test_views: scope |
| Внутренний черновик после 409/422; F01 | bound CommandForm, detail partials | test_ui_remediation: private_draft_*; browser_ui_remediation |
| Актуальный доступ к уведомлениям; F12 | permissions.visible_notifications | test_ui_remediation: internal_legacy_notification_hidden_after_access_revocation |
| Тип обращения ограничивает решения; F03 | services.COMMAND_TYPE_MATRIX, UI actions | test_domain_remediation: praise_cannot_be_approved_or_implemented; test_ui_remediation: praise_shows_only_allowed_decision_actions |
| Стабильный импорт и происхождение; F04 | load_demo, ImportBatch/Record, category identity | test_import_remediation: reorder, legacy PK, staged versions, immutable snapshots |
| Координатор продолжает/продлевает уточнение; F05 | resume_info/extend_info, forms | test_domain_remediation и test_ui_remediation: resume/extension |
| Согласование отзыва начатой работы; F06 | withdraw, pending request in current cycle | test_ui_remediation: withdrawal_request_is_reviewable_and_executable_by_coordinator |
| Видимая история прошлых результатов; F07 | ProcessingCycle и detail_cycles partial | test_ui_remediation: prior_cycle_result_and_evidence_visible_after_reopen |
| Проверка действующего исполнителя; F08 | eligible_assignee, assign, approve | test_domain_remediation: revoked_role_queue_and_clearance, started_implementation_cannot_be_unassigned |
| Видимая очередь ошибок доставки; F09 | failed_deliveries, retry_delivery | test_ui_remediation и test_domain_remediation: scoped retry, reason, deduplication |
| Оба входа ограничены; F10 | MutationThrottle, hashed shared buckets | test_operations_remediation: admin_and_regular_login_share_ip_limit, account_limit_survives_ip_changes |
| Ответственность восстанавливается по истории; F11 | ASSIGN payload before/after/reason | test_domain_remediation: assignment_event_preserves_before_after_and_reason |
| Календарь цикла неизменен; F13 | cycle.policy, sla helpers | test_domain_remediation: reopen/pause_after_timezone_change |
| Поздний worker не закрывает без напоминания; F14 | materialized reminder schedule, actual delivery grace | test_domain_remediation: late_worker/failed_reminder/stale_generation; test_import_remediation: fresh_import_schedules_reminders |
| Актуальный граф дубликатов; F15 | TicketRelation.active/archived_at, graph lock | test_domain_remediation: reopened_duplicate_archives_relation_and_allows_reverse_link |
| Коллизия номера обработана; F16 | 112 UUID bits, savepoint retry | test_domain_remediation: number_collision_retries, exhausted_number_collisions |
| Автор/исполнитель/очередь в фильтрах; F17 | TicketFilterForm и scoped lists | test_ui_remediation: author_assignee_and_queue_filters_use_visible_objects |
| Переклассификация с основанием; F18 | reclassify, saved calendar, route audit | test_domain_remediation: reclassification_*, priority_change_preserves_manual_queue_and_assignee |
| Категория «Не знаю»; F19 | migration0004, bootstrap_initial, fallback queues | test_import_remediation: production bootstrap / unknown fallback |
| Когорта, срез и корректные KPI; F20 | analytics, DB aggregates/percentiles, event snapshots | test_ui_remediation: cohort/historical_*; test_views: KPI totals and source dates |
| Нет цикла SQL на каждую карточку; F21 | current_cycle respects prefetch | test_ui_remediation: current_cycle_prefetch_avoids_per_card_query |
| Поиск основной карточки за пределами первых100; F22 | canonical_search, pagination | test_ui_remediation: canonical_search_reaches_beyond_first_hundred_and_is_scoped |
| Мобильное меню и клавиатура; F23 | inert, focus trap/return, Escape | browser_ui_remediation: 4 scenarios |
| Ответ автора при потере исполнителя; F24 повторного аудита | answer_info, replacement/assignment hold, explicit coordinator assign | test_domain_remediation: lost/unassigned recipient; test_ui_remediation: assignment marker |
| Legacy historical SQL NULL; F25 повторного аудита | NULL-safe transitions and documented unknown state | test_ui_remediation: legacy_snapshot_restores_implementation_after_answer |
| Идемпотентность и конкурентная версия | services atomic writes/lock, IdempotencyRecord | test_workflow; test_concurrency on PostgreSQL |
| Маршруты не переписывают прошлое | RoutingRule.supersedes, immutable save, revise_routing_rule | test_domain_remediation: routing_revision_preserves_immutable_source_version |
| Безопасная эксплуатация | TLS profile, runtime DML role, health/metrics/backup | test_operations_remediation, test_db_bootstrap, test_backup_tools, production_probe |
| Воспроизводимая нагрузка | guarded isolated load_check, actual EXPLAIN | docs/load-testing.md; JSON отчёт конкретного запуска |

Полномочия и обязательные поля каждой команды: [command-matrix.md](command-matrix.md).
Протокол повторного аудита должен отдельно указывать непроверенные промышленные
гарантии, даже если функциональные регрессии проходят.

## Доработки к защите хакатона

Карта именно исходного ТЗ находится в [пакете приёмки](hackathon/acceptance.md).
Сводка текущих прямых повторов и двусторонние ссылки: `feedback/recurrence.py`,
`views.recurring_problems`, `tests/test_recurrence.py`, `tests/browser_recurrence.py`.
Отдельный сохраняющий работу демо-набор: `load_hackathon_demo`.
Новые результаты проверки: [протокол P0/P1](hackathon/verification.md).
