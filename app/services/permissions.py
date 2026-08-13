"""权限点工具（ADR D18）。"""

from __future__ import annotations


def effective_permissions(user_permissions: list[str] | None, enterprise_permissions: list[str]) -> set[str]:
    """用户权限集：用户级覆盖优先，NULL 继承企业默认集。"""
    if user_permissions is not None:
        return set(user_permissions)
    return set(enterprise_permissions)


def has_permission(user_permissions: set[str], required: str) -> bool:
    return required in user_permissions
