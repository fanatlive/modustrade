"""Plain-language read model. Does not mutate workflow or deadlines."""
from django.utils import timezone
from .sla import reopen_deadline

RESULT_SOURCE = 'HACKATHON-RESULT-V1'


def has_demo_result(ticket):
    return (ticket.is_demo and ticket.source_record_id in {'HACKATHON-V1-1', RESULT_SOURCE}
            and ticket.status == 'CLOSED' and ticket.resolution_code == 'IMPLEMENTED')


def author_summary(ticket, cycle, info):
    due = None
    due_label = 'Следующий срок'
    action, anchor = 'Написать ответственным', '#conversation'
    reopen_until = reopen_deadline(ticket.closed_at, policy=cycle.policy if cycle else None) if ticket.closed_at else None
    can_reopen = bool(reopen_until and timezone.now() <= reopen_until)
    if ticket.status == 'CLOSED':
        title = 'Результат рассмотрения готов'
        message = 'Прочитайте итог ниже. Если вопрос не решён, укажите, что требует пересмотра.' if can_reopen else 'Срок самостоятельного повторного открытия истёк. Для пересмотра обратитесь к координатору очереди.'
        if ticket.resolution_code == 'DUPLICATE':
            title = 'Ваше обращение связано с основной темой'
            message = 'Закрыта только повторная карточка. Статус общей работы смотрите в доступном основном обращении ниже.'
        action, anchor = 'Прочитать результат', '#result'
        due, due_label = reopen_until, 'Повторное открытие автором до'
    elif ticket.status == 'WAITING_INFO' and info:
        title = 'Нужен ваш ответ'
        message = 'Ответьте на вопрос ниже, чтобы ответственные могли продолжить рассмотрение.'
        due, due_label = info.due_at, 'Ответить до'
        action, anchor = 'Ответить на уточнение', '#info-answer'
    elif cycle and cycle.policy.get('assignment_resume_status') == 'IN_PROGRESS':
        title = 'Координатор назначает исполнителя'
        message = 'Ваш ответ сохранён. Критерий результата и срок выполнения продолжают действовать.'
        due, due_label = ticket.plan_due_at, 'План выполнения'
    elif ticket.status == 'IN_PROGRESS':
        title = 'Решение принято, работа выполняется'
        message = 'Исполнитель должен выполнить согласованный результат и зафиксировать подтверждение. Сейчас от вас ничего не требуется.'
        due, due_label = ticket.plan_due_at, 'План выполнения'
    else:
        title = 'Обращение зарегистрировано' if ticket.status == 'NEW' else 'Ответственные рассматривают обращение'
        message = 'Сейчас от вас ничего не требуется. Здесь появятся вопрос, решение или результат.'
        if cycle:
            due = cycle.decision_due_at if cycle.first_response_at else cycle.first_response_due_at
            due_label = 'Решение ожидается до' if cycle.first_response_at else 'Первый ответ ожидается до'
    return {'title': title, 'message': message, 'due': due, 'due_label': due_label,
            'overdue': bool(ticket.status != 'CLOSED' and due and due < timezone.now()),
            'action': action, 'anchor': anchor, 'can_reopen': can_reopen}


def staff_summary(ticket, cycle, info, *, can_coordinate, awaiting_reassignment, pending_withdraw, type_commands):
    """What the processor or coordinator should do next, in one sentence."""
    due, due_label = None, ''
    if pending_withdraw and can_coordinate:
        title = 'Автор просит отозвать обращение'
        message = 'Согласуйте остановку работ или продолжите работу, если отзыв сейчас невозможен.'
    elif awaiting_reassignment:
        title = 'Назначьте исполнителя' if can_coordinate else 'Ждём назначения исполнителя'
        message = ('Ответ автора получен, решение и срок выполнения действуют. Выберите, кто продолжит реализацию.'
                   if can_coordinate else 'Координатор должен назначить исполнителя, чтобы продолжить реализацию.')
        due, due_label = ticket.plan_due_at, 'Выполнить до'
    elif ticket.status == 'NEW':
        title = 'Новое обращение ждёт разбора'
        message = 'Начните рассмотрение: автор увидит, что обращением занялись.'
        if cycle:
            due, due_label = cycle.first_response_due_at, 'Первый ответ до'
    elif ticket.status == 'REVIEW':
        if {'approve', 'decline'} & type_commands:
            title = 'Примите решение'
            message = ('Одобрите, если предложение стоит выполнить, или откажите с объяснением для автора. '
                       'Если сведений не хватает, задайте автору вопрос.')
        elif 'acknowledge' in type_commands:
            title = 'Обработайте положительный отзыв'
            message = 'Зафиксируйте, кому передана благодарность, и закройте обращение.'
        else:
            title = 'Продолжите рассмотрение'
            message = 'Решение уже принято. Уточните детали с автором или передайте обращение исполнителю.'
        if cycle:
            due, due_label = cycle.decision_due_at, 'Решение до'
    elif ticket.status == 'WAITING_INFO':
        title = 'Ждём ответа автора'
        message = 'Срок решения на паузе, пока автор отвечает на вопрос.'
        if can_coordinate:
            message += ' Если ответа нет, можно продлить срок или продолжить без него.'
        if info:
            due, due_label = info.due_at, 'Ответ автора до'
    elif ticket.status == 'IN_PROGRESS':
        title = 'Выполните работу и подтвердите результат'
        message = 'Когда результат достигнут по согласованному критерию, зафиксируйте его. Автор получит уведомление.'
        due, due_label = ticket.plan_due_at, 'Выполнить до'
    else:
        return None
    return {'title': title, 'message': message, 'due': due, 'due_label': due_label,
            'overdue': bool(due and due < timezone.now())}
