"""Resolve WebUI-managed paths without allowing escape from plugin data."""

from pathlib import Path

from ..security import validate_group as validate_group



def managed_path(root: Path, name: str, *, suffix: str = "") -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("无效的文件路径")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("无效的文件路径")
    relative = Path(name)
    if relative.is_absolute() or any(ord(char) < 32 for char in name):
        raise ValueError("无效的文件路径")
    if suffix and not name.endswith(suffix):
        relative = Path(name + suffix)
    base = root.resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base) or target == base:
        raise ValueError("文件路径超出受管目录")
    return target
