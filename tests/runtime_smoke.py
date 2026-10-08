"""Manual smoke test against an installed AstrBot without touching its data."""

import asyncio
import os
import shutil
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace


async def main():
    plugin_parent = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(plugin_parent))
    source_root = os.environ.get("ASTRBOT_SOURCE_ROOT")
    if source_root:
        sys.path.insert(0, source_root)

    temp_base = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="ww_gacha_runtime_") as directory:
        data_root = Path(directory).resolve()
        assert data_root.parent == temp_base
        existing_data = os.environ.get("ASTRBOT_PLUGIN_DATA_SOURCE")
        if existing_data:
            shutil.copytree(Path(existing_data), data_root, dirs_exist_ok=True)
        previous_root = os.environ.get("ASTRBOT_ROOT")
        os.environ["ASTRBOT_ROOT"] = str(data_root)
        from astrbot.api.star import StarTools
        from astrbot_plugin_ww_gacha_sim.main import WutheringWavesGachaPlugin

        original_data_dir = StarTools.__dict__["get_data_dir"]
        StarTools.get_data_dir = classmethod(lambda cls, plugin_name=None: data_root)
        plugin = None
        try:
            routes = []
            context = SimpleNamespace(
                register_web_api=lambda *args: routes.append(args)
            )
            plugin = WutheringWavesGachaPlugin(
                context, {"enable_rendering": True}
            )
            await plugin.initialize()
            assert plugin.cdb.get_schema_version() == 2
            assert len(routes) == 19
            assert plugin._active_pools()
            assert len(plugin.wuwa_group.parent_group.sub_command_filters) == 8
            image_chain = await plugin._run_render(
                plugin._render_to_chain,
                plugin.renderer.render_pool_detail,
                plugin._active_pools()[0],
            )
            assert len(image_chain) == 1
            plugin.enable_rendering = False

            async def get_kv(*args, **kwargs):
                return None

            async def put_kv(*args, **kwargs):
                return None

            plugin.get_kv_data = get_kv
            plugin.put_kv_data = put_kv
            event = SimpleNamespace(
                get_platform_id=lambda: "smoke-platform",
                get_sender_id=lambda: "smoke-user",
                get_sender_name=lambda: "Smoke User",
                unified_msg_origin="smoke:private:smoke-user",
                message_obj=SimpleNamespace(message_id="smoke-1"),
                plain_result=lambda message: message,
                stop_event=lambda: None,
            )
            pools = [result async for result in plugin.list_card_pools(event)]
            draws = [result async for result in plugin.single_pull(event)]
            pity = [result async for result in plugin.pity_status(event)]
            diagnostics = [result async for result in plugin.diagnose(event)]
            assert "当前可用的卡池" in pools[0]
            assert "单次抽卡结果" in draws[0]
            assert "本组累计抽数：1" in pity[0]
            assert "数据库：可写" in diagnostics[0]
            print(
                f"AstrBot {version('AstrBot')}: init, 19 APIs, 8 commands, "
                "render, draw, pity, diagnosis OK"
            )
        finally:
            if plugin is not None:
                await plugin.terminate()
            StarTools.get_data_dir = original_data_dir
            if previous_root is None:
                del os.environ["ASTRBOT_ROOT"]
            else:
                os.environ["ASTRBOT_ROOT"] = previous_root


if __name__ == "__main__":
    asyncio.run(main())
