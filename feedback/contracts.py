"""Typed payload boundaries shared by command callers and tooling.

Runtime validation and authorization remain in the transactional service.
Optional fields are command-dependent, as documented in docs/command-matrix.md.
"""
from datetime import datetime
from typing import Any, Literal, TypedDict

CommandName = Literal['start', 'acknowledge', 'assign', 'approve', 'decline', 'ask_info', 'answer_info',
                      'resume_info', 'extend_info', 'comment', 'implement', 'withdraw',
                      'reopen', 'duplicate', 'no_response', 'reclassify']


class CommandPayload(TypedDict, total=False):
    reason: str
    message: str
    question: str
    visibility: Literal['public', 'internal']
    assignee_id: int | str
    queue_id: int | str
    category_id: int | str
    canonical_id: str
    effective_priority: Literal['LOW', 'MEDIUM', 'HIGH']
    request_type: Literal['PROBLEM', 'SUGGESTION', 'PRAISE']
    plan_due_at: str | datetime
    due_at: str | datetime
    criteria: str
    result: str
    evidence: str
    files: list[Any]


class AssignmentReview(TypedDict):
    ticket_id: str
    number: str
    previous_assignee_id: int | None
    assignee_id: int | None
    status: str
