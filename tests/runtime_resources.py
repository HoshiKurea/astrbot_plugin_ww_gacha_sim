"""Real AstrBot Web request/response, multipart and offline-render regression."""

import asyncio
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent))
if os.environ.get("ASTRBOT_SOURCE_ROOT"):
    sys.path.insert(0, os.environ["ASTRBOT_SOURCE_ROOT"])


def create_app(plugin, routes):
    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.applications import Starlette
    from starlette.routing import Route

    def endpoint(handler):
        async def receive(request):
            with bind_request_context(PluginRequest(request, username="resource-qa",
                                                   plugin_name=root.name, path_params=request.path_params)):
                return await handler(**request.path_params)
        return receive

    return Starlette(routes=[Route(re.sub(r"<(\w+)>", r"{\1}", route), endpoint(handler), methods=methods)
                             for route, handler, methods, _ in routes])


async def configure(plugin):
    selected = [next(item for item in plugin.item_manager.get_item_objects().values() if item.name == name)
                for name in ("凌阳", "丹瑾", "远行者佩枪·洞察")]
    content = plugin._active_pools()[0].to_dict()
    content.update(cp_id="resource-qa", name="资源验收卡池", config_group="default",
                   included_item_ids={rarity: [item.external_id for item in selected if item.rarity == rarity]
                                      for rarity in ("3star", "4star", "5star")}, rate_up_item_ids={})
    await plugin.gacha_service.run(plugin.pool_service.save, "resource-qa.json", content, plugin.pool_service.revision)
    return selected


async def wait_job(client, job):
    for _ in range(300):
        response = await client.get(f"/{root.name}/resources/status", params={"job_id": job["id"]})
        assert response.status_code == 200, response.text
        current = response.json()["job"]
        if current["state"] != "running":
            assert current["state"] == "done", current
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("Resource job did not complete")


async def main():
    import httpx
    from PIL import Image
    with tempfile.TemporaryDirectory(prefix="ww_resources_runtime_") as directory:
        previous_root = os.environ.get("ASTRBOT_ROOT")
        os.environ["ASTRBOT_ROOT"] = directory
        from astrbot.api.star import StarTools
        from astrbot_plugin_ww_gacha_sim.main import WutheringWavesGachaPlugin
        original_data_dir = StarTools.__dict__["get_data_dir"]
        plugin = None
        try:
            online_root = Path(directory) / "online"
            StarTools.get_data_dir = classmethod(lambda cls, plugin_name=None: online_root)
            routes = []
            plugin = WutheringWavesGachaPlugin(SimpleNamespace(register_web_api=lambda *args: routes.append(args)), {})
            await plugin.initialize()
            selected = await configure(plugin)
            output = io.BytesIO()
            Image.new("RGBA", (180, 260), (80, 150, 130, 220)).save(output, "PNG")
            downloads = []
            def download(source, **kwargs):
                downloads.append(source)
                return output.getvalue()
            plugin.rs_loader.download_with_retry = download
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(plugin, routes)), base_url="http://test") as client:
                reply = await client.post(f"/{root.name}/resources/prepare", json={"pool_id": "resource-qa"})
                assert reply.status_code == 200, reply.text
                await wait_job(client, reply.json()["job"])
                assert len(downloads) == 3
                for item in selected:
                    reply = await client.post(f"/{root.name}/portraits/preview", json={"source": item.portrait_url})
                    assert reply.status_code == 200 and reply.json()["data_url"]
                reply = await client.post(f"/{root.name}/portraits/import", json={"group": "default", "source": selected[0].portrait_url})
                assert reply.status_code == 200, reply.text
                assert len(downloads) == 3
                reply = await client.get(f"/{root.name}/resources/export", params={"pool_id": "resource-qa"})
                assert reply.status_code == 200 and reply.content[:2] == b"PK", reply.text[:200] if reply.status_code != 200 else ""
                archive = reply.content
            await plugin.terminate()
            offline_root = Path(directory) / "offline"
            StarTools.get_data_dir = classmethod(lambda cls, plugin_name=None: offline_root)
            routes = []
            plugin = WutheringWavesGachaPlugin(SimpleNamespace(register_web_api=lambda *args: routes.append(args)), {})
            await plugin.initialize()
            await configure(plugin)
            def forbidden(*args, **kwargs):
                raise AssertionError("Offline operation contacted network")
            plugin.rs_loader.download_with_retry = forbidden
            app = create_app(plugin, routes)
            before = await plugin.gacha_service.history_count("qa", "qa", None)
            revision = plugin.pool_service.revision
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                reply = await client.post(f"/{root.name}/resources/import", files={"file": ("offline.zip", archive, "application/zip")})
                assert reply.status_code == 200, reply.text
                imported = await wait_job(client, reply.json()["job"])
                assert imported["ready"] == 3
                reply = await client.get(f"/{root.name}/resources/status", params={"pool_id": "resource-qa"})
                assert reply.status_code == 200 and reply.json()["pinned"] == 3
                plugin.lf_cache.clear_all_cache()
                for item in selected:
                    reply = await client.post(f"/{root.name}/portraits/preview", json={"source": item.portrait_url})
                    assert reply.status_code == 200, reply.text
                # Request parsing stays on the owner loop; concurrent requests
                # cannot accidentally share context or use another request body.
                replies = await asyncio.gather(*(client.get(f"/{root.name}/health") for _ in range(4)))
                assert all(reply.status_code == 200 for reply in replies)
            assert plugin.pool_service.revision == revision
            assert await plugin.gacha_service.history_count("qa", "qa", None) == before
            scene = await plugin.render_service.run(plugin.renderer.render_ten_pulls, selected * 3 + selected[:1])
            assert scene.width > 1000 and scene.mode == "RGB"
            print(json.dumps({"result": "real Web APIs, background preparation, shared previews/imports, ZIP download, multipart offline import, cache-clear survival, offline rendering passed", "downloads": len(downloads), "package_bytes": len(archive), "routes": len(routes)}, ensure_ascii=False))
        finally:
            if plugin is not None:
                await plugin.terminate()
            StarTools.get_data_dir = original_data_dir
            if previous_root is None:
                os.environ.pop("ASTRBOT_ROOT", None)
            else:
                os.environ["ASTRBOT_ROOT"] = previous_root


if __name__ == "__main__":
    asyncio.run(main())
