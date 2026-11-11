"""Portable generated search document; PostgreSQL owns tokenization at write time."""
from django.contrib.postgres.search import SearchVectorField
from django.db.models import Func


class TicketSearchDocument(Func):
    arity = 2
    output_field = SearchVectorField()

    def as_postgresql(self, compiler, connection, **extra_context):
        description, description_params = compiler.compile(self.source_expressions[0])
        solution, solution_params = compiler.compile(self.source_expressions[1])
        # An explicit regconfig makes to_tsvector immutable, as required for a
        # persisted generated column; no session text-search setting is used.
        return (f"(to_tsvector('russian'::regconfig, COALESCE({description}, '')) || "
                f"to_tsvector('russian'::regconfig, COALESCE({solution}, '')))",
                [*description_params, *solution_params])

    def as_sqlite(self, compiler, connection, **extra_context):
        # SQLite demo queries use icontains and never query this placeholder.
        return "''", []
