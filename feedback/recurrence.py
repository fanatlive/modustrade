"""Current, direct duplicate links; both endpoints must be visible."""
from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from .models import TicketRelation
from .permissions import visible_tickets


def visible_relations(user):
    scope = visible_tickets(user).values("pk")
    return TicketRelation.objects.filter(active=True, ticket_id__in=scope, canonical_id__in=scope)


def recurring_tickets(user, *, demo, date_from=None, date_to=None):
    scope = visible_tickets(user).filter(is_demo=demo)
    repeats = scope
    if date_from:
        repeats = repeats.filter(submitted_at__date__gte=date_from)
    if date_to:
        repeats = repeats.filter(submitted_at__date__lte=date_to)
    counts = visible_relations(user).filter(canonical_id=OuterRef("pk"), ticket_id__in=repeats.values("pk")).order_by().values("canonical_id").annotate(n=Count("pk")).values("n")
    return scope.select_related("category", "assignee", "queue").annotate(
        repeat_count=Coalesce(Subquery(counts, output_field=IntegerField()), 0)
    ).filter(repeat_count__gt=0).order_by("-repeat_count", "number")
