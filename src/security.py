"""Shared validation for identifiers derived from user-managed configuration."""

import re

GROUP_PATTERN = re.compile(r"[\w\u4e00-\u9fff-]{1,64}\Z", re.UNICODE)


def validate_group(group: str) -> str:
    if not isinstance(group, str) or not GROUP_PATTERN.fullmatch(group):
        raise ValueError("配置组名称只能包含字母、数字、下划线、连字符或中文，长度不超过 64")
    return group
