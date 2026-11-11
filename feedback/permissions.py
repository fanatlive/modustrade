"""One shared object-scope predicate for UI, commands, and delivery."""
from django.db.models import BooleanField, CharField, Func, Q
from django.db.models.functions import Cast
from .models import Notification, Ticket, Queue, User


def has_role(user, *roles):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    valid = set(User.Role.values)
    current = {user.role} if isinstance(user.role, str) and user.role in valid else set()
    extras = user.extra_roles
    # Existing malformed JSON must neither crash authorization nor grant a role
    # by iterating dictionary keys or the characters of a string.
    if isinstance(extras, list) and all(isinstance(role, str) and role in valid for role in extras):
        current.update(extras)
    return bool(current.intersection(roles))


def staff_scope(user, prefix=""):
    if not user or not user.is_authenticated or not user.is_active or not (user.is_superuser or has_role(user, "processor", "coordinator", "manager")):
        return Q(pk__in=[])
    # Semi-joins preserve one outer row per ticket even when a queue has many
    # members. DISTINCT on full ticket bodies would sort every visible card.
    owned = Queue.objects.filter(owner=user).values("pk")
    member = Queue.members.through.objects.filter(user_id=user.pk).values("queue_id")
    return (Q(**{prefix + "assignee_id": user.pk})
            | Q(**{prefix + "queue_id__in": owned})
            | Q(**{prefix + "queue_id__in": member}))


def _visible_scope(user, prefix=""):
    staff = staff_scope(user, prefix)
    if not user.restricted_access:
        staff &= Q(**{prefix + "confidentiality": Ticket.Confidentiality.STANDARD})
    return Q(**{prefix + "author_id": user.pk}) | staff


def visible_tickets(user):
    if not user or not user.is_authenticated or not user.is_active:
        return Ticket.objects.none()
    if user.is_superuser:
        return Ticket.objects.defer("search_document")
    return Ticket.objects.filter(_visible_scope(user)).defer("search_document")


def can_process(user, ticket):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    if not has_role(user, "processor", "coordinator", "manager"):
        return False
    if ticket.confidentiality == Ticket.Confidentiality.RESTRICTED and not user.restricted_access:
        return False
    return ticket.assignee_id == user.pk or ticket.queue.owner_id == user.pk or ticket.queue.members.filter(pk=user.pk).exists()


def can_coordinate(user, ticket=None):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    if not has_role(user, "coordinator", "manager"):
        return False
    if ticket is not None:
        return can_process(user, ticket)
    return Queue.objects.filter(Q(owner=user) | Q(members=user)).exists()


def can_view_internal(user, ticket):
    # Authorship always takes precedence over an additional operational role.
    return ticket.author_id != getattr(user, "pk", None) and can_process(user, ticket)


class _ValidAdditionalRoles(Func):
    """Validate legacy JSON in SQL without materializing the staff directory."""
    output_field = BooleanField()

    def __init__(self):
        super().__init__("extra_roles")

    def as_postgresql(self, compiler, connection, **extra_context):
        column, params = compiler.compile(self.source_expressions[0])
        choices = ", ".join(["%s"] * len(User.Role.values))
        return (f"(jsonb_typeof({column}) = 'array' AND NOT EXISTS ("
                f"SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({column}) = 'array' THEN {column} ELSE '[]'::jsonb END) er(value) "
                f"WHERE jsonb_typeof(er.value) <> 'string' OR er.value #>> '{{}}' NOT IN ({choices})))",
                [*params, *params, *params, *User.Role.values])

    def as_sqlite(self, compiler, connection, **extra_context):
        column, params = compiler.compile(self.source_expressions[0])
        choices = ", ".join(["%s"] * len(User.Role.values))
        return (f"(json_type({column}) = 'array' AND NOT EXISTS ("
                f"SELECT 1 FROM json_each(CASE WHEN json_type({column}) = 'array' THEN {column} ELSE '[]' END) er "
                f"WHERE er.type <> 'text' OR er.value NOT IN ({choices})))",
                [*params, *params, *params, *User.Role.values])


def operational_users():
    """Portable role predicate, including additional roles stored as JSON."""
    return User.objects.filter(is_active=True).alias(_extra=Cast("extra_roles", CharField()), _valid_extra=_ValidAdditionalRoles()).filter(
        Q(is_superuser=True) | Q(role__in=["processor", "coordinator", "manager"])
        | (Q(_valid_extra=True) & (Q(_extra__contains='"processor"') | Q(_extra__contains='"coordinator"') | Q(_extra__contains='"manager"'))))


def visible_notifications(user):
    """Delivery never grants lasting access to a revoked internal event."""
    if not user or not user.is_authenticated or not user.is_active:
        return Notification.objects.none()
    items = Notification.objects.filter(user=user)
    internal = ~Q(ticket__author_id=user.pk)
    if not user.is_superuser:
        items = items.filter(_visible_scope(user, "ticket__"))
        internal &= staff_scope(user, "ticket__")
        if not user.restricted_access:
            internal &= Q(ticket__confidentiality=Ticket.Confidentiality.STANDARD)
    return items.filter(Q(outbox__event__visibility="public") | (Q(outbox__event__visibility="internal") & internal))
