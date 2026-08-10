from __future__ import annotations

from app.constants import Permission
from app.services.permissions import effective_permissions, has_permission


def test_default_permissions_exclude_admin_only():
    assert Permission.QUOTE_APPLY in Permission.DEFAULT
    assert Permission.DELIVERABLE_EXPORT in Permission.DEFAULT
    assert Permission.REVIEW_PROVIDER_CONFIG not in Permission.DEFAULT
    assert Permission.ADMIN_USER not in Permission.DEFAULT
    assert Permission.ADMIN_QUOTA not in Permission.DEFAULT
    assert Permission.AUDIT_VIEW not in Permission.DEFAULT


def test_effective_permissions_inherit_or_override():
    assert effective_permissions(None, ["a", "b"]) == {"a", "b"}
    assert effective_permissions(["x"], ["a"]) == {"x"}


def test_has_permission():
    assert has_permission({"quote.apply"}, Permission.QUOTE_APPLY)
    assert not has_permission({"file.read"}, Permission.QUOTE_APPLY)
