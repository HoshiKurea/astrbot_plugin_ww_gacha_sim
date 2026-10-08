"""Validated plugin settings; changes take effect after a plugin reload."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginSettings:
    enable_rendering: bool = True
    enable_history_recording: bool = True
    save_rendered_results: bool = False
    cache_cleanup_interval: int = 24
    enable_proxy: bool = False
    proxy_url: str = ""
    default_pool_id: str = ""
    render_workers: int = 2
    render_queue_limit: int = 20
    render_timeout_seconds: int = 8
    portrait_download_workers: int = 3
    portrait_wait_timeout_seconds: int = 45
    database_queue_limit: int = 64
    cache_max_mb: int = 512
    rendered_result_retention_days: int = 7
    font_path: str = ""

    @classmethod
    def from_config(cls, config) -> "PluginSettings":
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            default = field.default
            raw = config.get(name, default)
            if isinstance(default, bool):
                if not isinstance(raw, bool):
                    raise ValueError(f"{name} 必须是布尔值")
            elif isinstance(default, int):
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise ValueError(f"{name} 必须是整数")
            elif not isinstance(raw, str):
                raise ValueError(f"{name} 必须是字符串")
            values[name] = raw
        settings = cls(**values)
        ranges = {
            "cache_cleanup_interval": (1, 720),
            "render_workers": (1, 4),
            "render_queue_limit": (0, 100),
            "render_timeout_seconds": (1, 60),
            "portrait_download_workers": (1, 6),
            "portrait_wait_timeout_seconds": (5, 120),
            "database_queue_limit": (0, 256),
            "cache_max_mb": (16, 4096),
            "rendered_result_retention_days": (1, 365),
        }
        for name, (low, high) in ranges.items():
            value = getattr(settings, name)
            if not low <= value <= high:
                raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")
        if settings.font_path and not Path(settings.font_path).is_file():
            raise ValueError("font_path 指向的字体文件不存在")
        return settings
