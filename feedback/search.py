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


STOPWORDS = {"который", "которая", "которые", "нужно", "можно", "чтобы", "также", "через", "этого", "после", "перед", "сотрудников", "обращение", "предлагаю"}


def _keywords(text: str) -> list[str]:
    return list(dict.fromkeys(word.casefold() for word in re.findall(r"[^\W\d_]{4,}", text) if word.casefold() not in STOPWORDS))[:12]


def _any_word(tickets: QuerySet[Ticket], words: list[str]) -> QuerySet[Ticket]:
    if connection.vendor == "postgresql":
        query = reduce(or_, (SearchQuery(word, config="russian") for word in words))
        return tickets.alias(_search_vector=search_vector()).filter(_search_vector=query).annotate(
            relevance=SearchRank(search_vector(), query)).order_by("-relevance", "number")
    return tickets.filter(reduce(or_, (Q(description__icontains=word) | Q(proposed_solution__icontains=word) for word in words))).order_by("number")


def similar_tickets(tickets: QuerySet[Ticket], ticket: Ticket) -> QuerySet[Ticket]:
    # Suggestions never change the duplicate graph. The user checks the meaning.
    words = _keywords(ticket.description)
    tickets = tickets.exclude(pk=ticket.pk)
    if not words:
        return tickets.none()
    return _any_word(tickets, words)


def suggest_similar(tickets: QuerySet[Ticket], text: str, *, limit: int = 5) -> list[Ticket]:
    """Short, ranked list of likely repeats inside an already-authorized queryset.

    A single shared word is noise, so a candidate needs at least two of the
    text's keywords. Stems (first six letters) tolerate Russian endings on both
    database backends; PostgreSQL additionally pre-filters with morphology.
    """
    words = _keywords(text[:2000])
    if not words:
        return []
    stems = list(dict.fromkeys(word[:6] for word in words))
    required = 2 if len(stems) >= 3 else 1
    scored = []
    for candidate in _any_word(tickets, words).only("pk", "number", "description", "proposed_solution", "status")[:200]:
        haystack = f"{candidate.description} {candidate.proposed_solution}".casefold()
        score = sum(stem in haystack for stem in stems)
        if score >= required:
            scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], item[1].number))
    return [candidate for _, candidate in scored[:limit]]
