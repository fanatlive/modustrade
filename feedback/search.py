"""Scoped full-text search. Callers supply an already-authorized queryset."""
from __future__ import annotations

import re
from functools import reduce
from operator import or_

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import connection
from django.db.models import F, Q, QuerySet

from .models import Ticket


def search_vector():
    # PostgreSQL maintains this persisted vector when source text changes.
    return F("search_document")


def scoped_search(tickets: QuerySet[Ticket], query: str) -> QuerySet[Ticket]:
    query = query.strip()[:200]
    if not query:
        return tickets
    identifiers = Q(number__icontains=query) | Q(source_record_id__icontains=query)
    if connection.vendor == "postgresql":
        return tickets.alias(_search_vector=search_vector()).filter(
            identifiers | Q(_search_vector=SearchQuery(query, config="russian", search_type="websearch")))
    return tickets.filter(identifiers | Q(description__icontains=query) | Q(proposed_solution__icontains=query))


def similar_tickets(tickets: QuerySet[Ticket], ticket: Ticket) -> QuerySet[Ticket]:
    # Suggestions never change the duplicate graph. The user checks the meaning.
    stopwords = {"который", "которая", "которые", "нужно", "можно", "чтобы", "также", "через", "этого", "после", "перед", "сотрудников", "обращение", "предлагаю"}
    words = list(dict.fromkeys(word.casefold() for word in re.findall(r"[^\W\d_]{4,}", ticket.description) if word.casefold() not in stopwords))[:12]
    tickets = tickets.exclude(pk=ticket.pk)
    if not words:
        return tickets.none()
    if connection.vendor == "postgresql":
        query = reduce(or_, (SearchQuery(word, config="russian") for word in words))
        return tickets.alias(_search_vector=search_vector()).filter(_search_vector=query).annotate(
            relevance=SearchRank(search_vector(), query)).order_by("-relevance", "number")
    return tickets.filter(reduce(or_, (Q(description__icontains=word) | Q(proposed_solution__icontains=word) for word in words))).order_by("number")
