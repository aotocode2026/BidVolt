from __future__ import annotations

from app.constants import PROJECT_TRANSITIONS, ProjectStatus


def test_valid_transitions():
    assert ProjectStatus.PROCESSING in PROJECT_TRANSITIONS[ProjectStatus.DRAFT]
    assert ProjectStatus.PARTIAL_DONE in PROJECT_TRANSITIONS[ProjectStatus.PROCESSING]
    assert ProjectStatus.DONE in PROJECT_TRANSITIONS[ProjectStatus.PARTIAL_DONE]
    assert ProjectStatus.ARCHIVED in PROJECT_TRANSITIONS[ProjectStatus.DONE]
    assert ProjectStatus.ARCHIVED in PROJECT_TRANSITIONS[ProjectStatus.DRAFT]


def test_invalid_transitions():
    assert ProjectStatus.DONE not in PROJECT_TRANSITIONS[ProjectStatus.DRAFT]
    assert ProjectStatus.PROCESSING not in PROJECT_TRANSITIONS[ProjectStatus.DONE]
    assert PROJECT_TRANSITIONS[ProjectStatus.ARCHIVED] == set()
