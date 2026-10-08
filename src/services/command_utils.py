"""Argument parsing and text fallback for public draw commands."""

from functools import wraps


def finish_command(handler):
    """Consume a matched command after its replies have passed the respond stage.

    Stopping before yield would suppress delivery in AstrBot's scheduler. Keep
    the original signature so its command filter can still parse arguments.
    """
    @wraps(handler)
    async def wrapped(self, event, *args, **kwargs):
        async for result in handler(self, event, *args, **kwargs):
            yield result
        event.stop_event()

    return wrapped


def parse_history_args(pool_or_page: str = "", page_text: str = "") -> tuple[str | None, int]:
    first = str(pool_or_page or "").strip()
    second = str(page_text or "").strip()
    if first.isdecimal():
        if second:
            raise ValueError("页码后不能再指定卡池；用法：/wwg 记录 [卡池ID或名称] [页码]")
        pool, raw_page = None, first
    else:
        pool, raw_page = first or None, second or "1"
    try:
        page = int(raw_page)
    except ValueError as exc:
        raise ValueError("页码必须是正整数") from exc
    if not 1 <= page <= 1_000_000:
        raise ValueError("页码必须在 1 到 1000000 之间")
    return pool, page


def format_history_text(records: list[dict], page: int, total_pages: int,
                        total: int, pool: str | None = None) -> str:
    title = f"抽卡记录 · {pool or '全部卡池'} · 第 {page}/{total_pages} 页（共 {total} 条）"
    lines = [title]
    stars = {"5star": "★★★★★", "4star": "★★★★", "3star": "★★★"}
    item_types = {"character": "角色", "weapon": "武器"}
    for record in records:
        item_type = item_types.get(record.get("type"), record.get("type") or "未知类型")
        lines.append(f"{item_type} · {stars.get(record['rarity'], record['rarity'])} "
                     f"{record['item']} · {record['pull_time']}")
    prefix = f"/wwg 记录 {pool} " if pool else "/wwg 记录 "
    if page > 1:
        lines.append(f"上一页：{prefix}{page - 1}")
    if page < total_pages:
        lines.append(f"下一页：{prefix}{page + 1}")
    return "\n".join(lines)
