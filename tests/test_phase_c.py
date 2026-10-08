"""Native Plugin Page API and distributable asset regression checks."""

import asyncio
import io
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


class FakeRequest:
    def __init__(self, username="admin", payload=None, files=None, query=None):
        self.username = username
        self.payload = payload or {}
        self._files = files or {}
        self.query = SimpleNamespace(get=lambda key, default=None, type=None:
                                     (type(query[key]) if type else query[key]) if query and key in query else default)

    async def json(self, default=None):
        return self.payload

    async def files(self):
        return self._files


@pytest.fixture
def native(tmp_path, monkeypatch):
    astrbot = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")
    api_mod.logger = logging.getLogger("phase_c")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: str(tmp_path))
    class Upload:
        def __init__(self, data):
            self.data = data
            self.content_length = len(data)
        async def read(self, size):
            return self.data[:size]
    web.PluginUploadFile = Upload
    web.json_response = lambda body, status_code=200: (status_code, body)
    web.error_response = lambda message, status_code=400, data=None: (
        status_code, {"message": message, "data": data})
    web.request = FakeRequest()
    for name, module in (("astrbot", astrbot), ("astrbot.api", api_mod),
                         ("astrbot.api.star", star), ("astrbot.api.web", web)):
        monkeypatch.setitem(sys.modules, name, module)

    import importlib
    import src.web.api as native_module
    importlib.reload(native_module)
    from src.db.database import CommonDatabase
    from src.db.item_db_operations import ItemDBOperations
    from src.gacha.cardpool_manager import CardPoolManager
    from src.services.pool_service import PoolService

    pool_dir = tmp_path / "pools"
    pool_dir.mkdir()
    config = {"cp_id": "sample", "name": "测试池", "config_group": "default",
              "enable": False, "probability_settings": {},
              "probability_progression": {}, "included_item_ids": {},
              "rate_up_item_ids": {}}
    (pool_dir / "sample.json").write_text(json.dumps(config), encoding="utf-8")
    db = CommonDatabase(tmp_path / "test.db")
    items = ItemDBOperations(db)
    pools = PoolService(CardPoolManager(pool_dir), items)
    service = native_module.NativeAdminAPI(pools, items, db)
    yield SimpleNamespace(module=native_module, service=service, pools=pools,
                          items=items, db=db, tmp_path=tmp_path, Upload=Upload,
                          config=config, web=web)
    asyncio.run(service.shutdown())
    db.close()


def invoke(native, method, payload=None, username="admin", **kwargs):
    native.module.request = FakeRequest(username, payload, kwargs.get("files"), kwargs.get("query"))
    return asyncio.run(getattr(native.service, method)(*kwargs.get("args", ())))


def test_routes_auth_and_revision_conflict(native):
    routes = []
    native.service.register(SimpleNamespace(register_web_api=lambda *args: routes.append(args)))
    assert len(routes) == 19
    assert all(route[0].startswith("/astrbot_plugin_ww_gacha_sim/") for route in routes)
    assert invoke(native, "list_pools", username=None)[0] == 401
    assert invoke(native, "list_pools", username="api_key:abc")[0] == 403
    listed = invoke(native, "list_pools")[1]
    assert listed["revision"] == native.pools.revision
    assert listed["pools"][0]["filename"] == "sample.json"
    assert invoke(native, "health")[1]["cache"] == "disabled"
    draft = dict(native.config, cp_id="", name="新卡池")
    result = invoke(native, "save_pool", {"filename": "new.json", "content": draft,
                                           "expected_revision": native.pools.revision})
    assert result[0] == 200
    assert native.pools.get(result[1]["pool"]["cp_id"]).name == "新卡池"
    assert "new.json" in {pool["filename"] for pool in invoke(native, "list_pools")[1]["pools"]}
    assert invoke(native, "save_pool", {"filename": "new.json", "content": draft,
                                         "expected_revision": 1})[0] == 409
    native.service.close()
    assert invoke(native, "health")[0] == 503


def test_item_reference_guard_and_revision(native):
    revision = native.pools.revision
    item = {"external_id": "hero-1", "name": "测试角色", "rarity": "5star",
            "type": "character", "affiliated_type": "冷凝", "portrait_url": ""}
    assert invoke(native, "save_item", {"group": "default", "item": item,
                                         "expected_revision": revision})[0] == 200
    assert invoke(native, "save_item", {"group": "default", "item": item,
                                         "expected_revision": revision})[0] == 409
    native.config["included_item_ids"] = {"5star": ["hero-1"]}
    native.pools.save("sample.json", native.config, native.pools.revision)
    assert invoke(native, "delete_item", {"group": "default", "external_id": "hero-1",
                                           "expected_revision": native.pools.revision})[0] == 400
    changed = dict(item, rarity="4star")
    assert invoke(native, "save_item", {"group": "default", "item": changed,
                                         "expected_revision": native.pools.revision})[0] == 400
    assert native.items.get_item_by_id("hero-1", "default_items")["rarity"] == "5star"


def test_failed_item_publication_restores_database(native, monkeypatch):
    item = {"external_id": "rollback-1", "name": "旧名称", "rarity": "4star",
            "type": "weapon", "portrait_url": ""}
    assert invoke(native, "save_item", {"group": "default", "item": item,
                                         "expected_revision": native.pools.revision})[0] == 200
    def fail_reload():
        raise RuntimeError("broken config")
    monkeypatch.setattr(native.pools, "reload", fail_reload)
    changed = dict(item, name="新名称")
    assert invoke(native, "save_item", {"group": "default", "item": changed,
                                         "expected_revision": native.pools.revision})[0] == 500
    assert native.items.get_item_by_id("rollback-1", "default_items")["name"] == "旧名称"
    assert invoke(native, "delete_item", {"group": "default", "external_id": "rollback-1",
                                           "expected_revision": native.pools.revision})[0] == 500
    assert native.items.get_item_by_id("rollback-1", "default_items") is not None


def test_upload_reencodes_and_rejects_invalid(native):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="JPEG")
    result = invoke(native, "upload_portrait", files={"file": native.Upload(output.getvalue())},
                    args=("default",))
    assert result[0] == 200
    portrait = result[1]["portrait_url"]
    assert portrait.startswith("local:default/")
    path = native.tmp_path / "portraits" / portrait.removeprefix("local:")
    with Image.open(path) as picture:
        assert picture.format == "WEBP"
    from src.render import ui_resources_manager
    ui_resources_manager.StarTools = SimpleNamespace(
        get_data_dir=lambda _: str(native.tmp_path))
    renderer = ui_resources_manager.UIResourceManager.__new__(
        ui_resources_manager.UIResourceManager)
    renderer.logger = logging.getLogger("phase_c_renderer")
    loaded = renderer.get_item_portrait(SimpleNamespace(
        external_id="hero", name="测试", portrait_url=portrait))
    assert loaded.size == (8, 8) and loaded.mode == "RGBA"
    assert invoke(native, "save_item", {"group": "default", "expected_revision": native.pools.revision,
        "item": {"external_id": "with-art", "name": "立绘角色", "rarity": "5star",
                 "type": "character", "portrait_url": portrait}})[0] == 200
    assert invoke(native, "upload_portrait", files={"file": native.Upload(b"not an image")},
                  args=("default",))[0] == 400
    assert invoke(native, "upload_portrait", files={"file": native.Upload(output.getvalue())},
                  args=("../escape",))[0] == 400


def test_page_build_has_no_network_dependencies():
    root = Path(__file__).resolve().parents[1]
    for name in ("index.html", "style.css", "app.js"):
        source = (root / "webui" / name).read_bytes()
        assert source == (root / "pages" / "admin" / name).read_bytes()
        safe = source.replace(b"http://www.w3.org/2000/svg", b"")
        assert b"https://" not in safe and b"http://" not in safe
    assert b"AstrBotPluginPage" in (root / "pages" / "admin" / "app.js").read_bytes()

def test_portrait_catalog_pins_version_and_walks_subdirectories(native, monkeypatch):
    from src.web.portrait_catalog import ART_PATH
    catalog = native.service.portraits
    calls = []
    sha = 'a' * 40
    def api(path):
        calls.append(path)
        if path.startswith('/commits/'):
            return {'sha': sha}
        if path.startswith('/git/trees/'):
            return {'truncated': False, 'tree': [{'type': 'blob', 'path': 'T_Luckdraw_new_UI.png'}]}
        return [{'type': 'dir', 'sha': 'b' * 40, 'path': ART_PATH + '/Role', 'name': 'Role'},
                {'type': 'file', 'path': ART_PATH + '/T_Luckdraw21010013_UI.png',
                 'name': 'T_Luckdraw21010013_UI.png'},
                {'type': 'file', 'path': '../escape.png', 'name': 'escape.png'}]
    monkeypatch.setattr(catalog, '_api', api)
    result = invoke(native, 'resource_portraits', query={'ref': '3.6'})
    assert result[0] == 200
    data = result[1]
    assert len(data['items']) == 2
    assert {row['type'] for row in data['items']} == {'weapon', 'character'}
    assert all('/' + sha + '/' in row['portrait_url'] for row in data['items'])
    assert all('?ref=' + sha in path for path in calls if '/contents/' in path)
    count = len(calls)
    assert catalog.catalog('3.6') == data
    assert len(calls) == count
    assert invoke(native, 'resource_portraits', query={'ref': '../bad'})[0] == 400


def test_portrait_preview_is_bounded_cached_and_authenticated(native, monkeypatch):
    from src.web.portrait_catalog import PortraitCatalog
    picture = io.BytesIO()
    Image.new('RGBA', (800, 400), 'teal').save(picture, format='PNG')
    calls = []
    def download(*args, **kwargs):
        calls.append(args[0])
        return picture.getvalue()
    monkeypatch.setattr(native.service.portraits.loader, 'download_with_retry', download)
    result = invoke(native, 'preview_portrait', {'source': 'https://example.org/art.png'})
    assert result[0] == 200
    import base64
    decoded = Image.open(io.BytesIO(base64.b64decode(result[1]['data_url'].split(',')[1])))
    assert decoded.size == (360, 180)
    invoke(native, 'preview_portrait', {'source': 'https://example.org/art.png'})
    assert len(calls) == 1
    for method in ['resource_versions', 'resource_portraits', 'preview_portrait']:
        assert invoke(native, method, username=None)[0] == 401
        assert invoke(native, method, username='api_key:test')[0] == 403
    assert invoke(native, 'preview_portrait', {'source': 'local:../secret.png'})[0] == 400
    assert invoke(native, 'preview_portrait', {'source': None})[0] == 400
    local = native.tmp_path / 'portraits' / 'default'
    local.mkdir(parents=True)
    (local / 'art.png').write_bytes(picture.getvalue())
    assert invoke(native, 'preview_portrait', {'source': 'local:default/art.png'})[0] == 200
    with pytest.raises(ValueError):
        PortraitCatalog(local).preview('file:///C:/private.png')

def test_fixed_upstream_supports_proxy_dns_without_following_redirects(native, monkeypatch):
    import httpx
    import src.web.portrait_catalog as module
    original = httpx.Client
    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
    monkeypatch.setattr(module.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(ValueError):
        native.service.portraits._api('/branches')
    assert len(requests) == 1
    for source in ['http://127.0.0.1/a.png', 'file:///private.png',
                   'https://api.github.com@127.0.0.1/private',
                   'https://raw.githubusercontent.com.evil.invalid/a.png']:
        assert not native.service.portraits.loader.is_valid_resource_url(source)

def test_github_quota_falls_back_to_pinned_public_tree(native, monkeypatch):
    from src.web.portrait_catalog import ART_PATH, GitHubRateLimit
    catalog = native.service.portraits
    sha = 'c' * 40
    monkeypatch.setattr(catalog, '_api', lambda path: (_ for _ in ()).throw(GitHubRateLimit('quota')))
    pages = []
    def page(path):
        pages.append(path)
        if path == '/branches/all':
            return {'branches': [{'name': '3.6', 'isDefault': True}, {'name': '3.5'}], 'has_more': False}
        if path.endswith('/Role'):
            assert '/tree/' + sha + '/' in path
            rows = [{'name': 'T_Luckdraw_new_UI.png', 'path': ART_PATH + '/Role/T_Luckdraw_new_UI.png', 'contentType': 'file'}]
        else:
            rows = [{'path': ART_PATH + '/Role', 'contentType': 'directory'},
                    {'path': '../bad', 'contentType': 'directory'}]
        return {'codeViewTreeRoute': {'refInfo': {'currentOid': sha}, 'tree': {'items': rows, 'totalCount': len(rows)}}}
    monkeypatch.setattr(catalog, '_page_payload', page)
    assert catalog.versions()['versions'] == ['3.6', '3.5']
    data = catalog.catalog('3.6')
    assert len(data['items']) == 1
    assert '/' + sha + '/' in data['items'][0]['portrait_url']
    catalog.catalog('3.6')
    assert len(pages) == 3


def test_github_quota_cooldown_and_transport_retry(native, monkeypatch):
    import httpx
    import src.web.portrait_catalog as module
    catalog = native.service.portraits
    original = httpx.Client
    calls = []
    def quota(request):
        calls.append(request)
        return httpx.Response(403, headers={'x-ratelimit-remaining': '0'})
    monkeypatch.setattr(module.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(quota), **kw))
    for _ in range(2):
        with pytest.raises(module.GitHubRateLimit):
            catalog._api('/branches')
    assert len(calls) == 1
    calls.clear()
    def transient(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError('disconnected')
        return httpx.Response(200, content=b'ok')
    monkeypatch.setattr(module.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(transient), **kw))
    assert catalog._download('https://github.com/' + module.REPOSITORY + '/branches/all') == b'ok'
    assert len(calls) == 2

def test_slow_catalog_is_polled_without_duplicate_jobs(native, monkeypatch):
    import threading
    release = threading.Event()
    calls = []
    def slow(ref):
        calls.append(ref)
        release.wait(3)
        return {'ref': ref, 'items': []}
    monkeypatch.setattr(native.service.portraits, 'catalog', slow)
    native.module.request = FakeRequest(query={'ref': '3.6'})
    async def run():
        try:
            assert (await native.service.resource_portraits())[1]['pending'] is True
            assert (await native.service.resource_portraits())[1]['pending'] is True
            assert calls == ['3.6']
            release.set()
            await asyncio.gather(*native.service.catalog_tasks.values())
            assert (await native.service.resource_portraits())[1]['items'] == []
            assert not native.service.catalog_tasks
        finally:
            release.set()
    asyncio.run(run())

def test_catalog_snapshot_survives_reload_and_expires(native, monkeypatch):
    import json
    from src.web.portrait_catalog import PortraitCatalog
    first = native.service.portraits
    data = {'ref': '3.6', 'commit': 'a' * 40, 'items': []}
    first._save_catalog_snapshot('3.6', data)
    second = PortraitCatalog(first.root)
    monkeypatch.setattr(second, '_api', lambda _: pytest.fail('snapshot should avoid network'))
    assert second.catalog('3.6') == data
    path = first._snapshot_path('3.6')
    record = json.loads(path.read_text())
    record['expires'] = 0
    path.write_text(json.dumps(record))
    assert first._read_catalog_snapshot('3.6') is None

def test_imported_portrait_renders_without_network(native, monkeypatch):
    picture = io.BytesIO()
    Image.new('RGBA', (120, 200), 'gold').save(picture, 'PNG')
    calls = []
    def download(url):
        calls.append(url)
        return picture.getvalue()
    monkeypatch.setattr(native.service.portraits, '_download', download)
    source = 'https://raw.githubusercontent.com/TomyJan/WutheringWaves-UIResources/3.6/a.png'
    status, result = invoke(native, 'import_portrait', {'group': 'default', 'source': source})
    assert status == 200 and result['portrait_url'].startswith('local:default/')
    file = native.tmp_path / 'portraits' / result['portrait_url'][6:]
    assert file.is_file()
    assert Image.open(file).size == (120, 200)
    assert invoke(native, 'import_portrait', {'group': 'default', 'source': source})[1] == result
    assert len(calls) == 1
    monkeypatch.setattr(native.service.portraits, '_download', lambda _: pytest.fail('offline render must not download'))
    from src.render import ui_resources_manager
    ui_resources_manager.StarTools = SimpleNamespace(get_data_dir=lambda _: str(native.tmp_path))
    manager = object.__new__(ui_resources_manager.UIResourceManager)
    manager.logger = logging.getLogger('offline_portrait_test')
    rendered = manager.get_item_portrait(SimpleNamespace(
        external_id='offline', name='离线角色', portrait_url=result['portrait_url']))
    assert rendered.size == (120, 200)
    assert invoke(native, 'preview_portrait', {'source': result['portrait_url']})[0] == 200
    assert invoke(native, 'import_portrait', {'group': 'default', 'source': 'file:///private.png'})[0] == 400


def test_failed_import_does_not_create_local_address(native, monkeypatch):
    monkeypatch.setattr(native.service.portraits, '_download', lambda _: None)
    result = invoke(native, 'import_portrait', {'group': 'default', 'source': 'https://example.org/missing.png'})
    assert result[0] == 400
    assert list((native.tmp_path / 'portraits').rglob('*.png')) == []
    assert invoke(native, 'import_portrait', {'group': '../private', 'source': 'https://example.org/a.png'})[0] == 400
    assert invoke(native, 'import_portrait', {'group': 'default', 'source': 'https://example.org/a.png'}, username=None)[0] == 401
