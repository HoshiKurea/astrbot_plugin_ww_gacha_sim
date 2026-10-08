"""Version-pinned upstream artwork and bounded, authenticated image previews."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image

from ..render.resource_loader import ResourceLoader
from ..render.image_encoding import encode_portrait
from .path_security import managed_path

REPOSITORY = "TomyJan/WutheringWaves-UIResources"
ART_PATH = "UIResources/Common/Image/Luckdraw"


class GitHubRateLimit(ValueError):
    """REST quota exhausted; public repository pages remain available."""


class PortraitCatalog:
    def __init__(self, root: Path, proxy=None, store=None):
        self.root, self.proxy = root, proxy
        self.store = store
        self.loader = ResourceLoader()
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.api_retry_at = 0.0
        self.stop = threading.Event()

    def close(self):
        self.stop.set()

    def _cached(self, key, build, ttl=600):
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(key)
                return cached[1]
        value = build()
        with self.lock:
            self.cache[key] = (time.monotonic() + ttl, value)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return value

    def _api(self, path):
        def load():
            if time.time() < self.api_retry_at:
                raise GitHubRateLimit("GitHub API 配额已耗尽，正在使用公开目录")
            return self._read_api(path)
        return self._cached("api:" + path, load)

    def _read_api(self, path):
        raw = self._download(
            f"https://api.github.com/repos/{REPOSITORY}{path}",
            max_bytes=4 * 1024 * 1024)
        if not raw:
            raise ValueError("无法读取 GitHub 素材目录，请检查网络、插件代理和版本；请求过多时请稍后重试。")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("素材仓库返回了无效目录") from exc

    def _download(self, url, max_bytes=10 * 1024 * 1024):
        if self.stop.is_set():
            raise ValueError("素材服务正在关闭")
        # Fixed upstream hosts also work with local proxy fake-IP DNS. Arbitrary
        # user URLs still use ResourceLoader's public-address checks. Redirects
        # are never followed, including on these allowlisted upstream hosts.
        parsed = urlsplit(url)
        if parsed.netloc == "raw.githubusercontent.com" and parsed.path.startswith(f"/{REPOSITORY}/"):
            return self.loader.download_with_retry(
                url, max_retries=2, timeout=12, proxy=self.proxy, max_bytes=max_bytes,
                stop_event=self.stop, total_budget_seconds=30,
            )
        trusted = parsed.scheme == "https" and (
            (parsed.netloc == "api.github.com" and parsed.path.startswith(f"/repos/{REPOSITORY}/"))
            or (parsed.netloc == "api.github.com" and parsed.path == f"/repos/{REPOSITORY}")
            or (parsed.netloc == "github.com" and parsed.path.startswith(f"/{REPOSITORY}/"))
            or (parsed.netloc == "raw.githubusercontent.com" and parsed.path.startswith(f"/{REPOSITORY}/")))
        if not trusted:
            return self.loader.download_with_retry(url, max_retries=1, timeout=12,
                                                   proxy=self.proxy, max_bytes=max_bytes,
                                                   stop_event=self.stop, total_budget_seconds=30)
        proxy = None
        if self.proxy:
            proxy = self.proxy.get("all://") or self.proxy.get("https://") or self.proxy.get("http://")
        for attempt in range(2):
            if self.stop.is_set():
                raise ValueError("素材服务正在关闭")
            try:
                with httpx.Client(timeout=20, proxy=proxy, follow_redirects=False) as client:
                    with client.stream("GET", url, headers={"User-Agent": "AstrBot-WW-Gacha"}) as response:
                        if parsed.netloc == "api.github.com" and response.status_code in (403, 429) and (
                                response.headers.get("x-ratelimit-remaining") == "0" or response.status_code == 429):
                            try:
                                reset = float(response.headers.get("x-ratelimit-reset", time.time() + 60))
                            except ValueError:
                                reset = time.time() + 60
                            self.api_retry_at = max(time.time() + 60, min(reset, time.time() + 3600))
                            raise GitHubRateLimit("GitHub API 配额已耗尽，正在使用公开目录")
                        response.raise_for_status()
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if self.stop.is_set():
                                raise ValueError("素材服务正在关闭")
                            data.extend(chunk)
                            if len(data) > max_bytes:
                                raise ValueError("素材超过大小限制")
                        return bytes(data)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (502, 503, 504) and attempt == 0:
                    continue
                message = "版本或素材不存在" if status == 404 else "访问被 GitHub 拒绝" if status == 403 else "GitHub 返回错误"
                raise ValueError(f"{message}（HTTP {status}），请核对版本或稍后重试") from exc
            except httpx.HTTPError as exc:
                if attempt == 0:
                    continue
                route = "插件代理" if proxy else "直连或系统网络"
                raise ValueError(f"GitHub 连接失败（{type(exc).__name__}，{route}），请检查对应代理设置后重试") from exc

    def versions(self):
        def build():
            source = "github-api"
            try:
                default = self._api("")["default_branch"]
                versions = [row["name"] for row in self._api("/branches?per_page=100")]
            except GitHubRateLimit:
                source = "github-page"
                branches = []
                for page in range(1, 6):
                    suffix = "" if page == 1 else f"?page={page}"
                    data = self._page_payload("/branches/all" + suffix)
                    branches.extend(data["branches"])
                    if not data.get("has_more"):
                        break
                default = next(row["name"] for row in branches if row.get("isDefault"))
                versions = [row["name"] for row in branches]
            return {"default": default, "versions": list(dict.fromkeys([default, *versions])), "source": source}
        return self._cached("versions", build)

    def catalog(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"[\w./-]{1,120}", ref) or ".." in ref:
            raise ValueError("请输入有效的版本分支、标签或提交编号")
        def build():
            saved = self._read_catalog_snapshot(ref)
            if saved is not None:
                return saved
            try:
                result = self._catalog_api(ref)
            except GitHubRateLimit:
                result = self._catalog_html(ref)
            self._save_catalog_snapshot(ref, result)
            return result
        return self._cached("catalog:" + ref, build)

    def _snapshot_path(self, ref):
        return self.root.parent / "portrait_catalog_cache" / (hashlib.sha256(ref.encode()).hexdigest() + ".json")

    def _read_catalog_snapshot(self, ref):
        try:
            path = self._snapshot_path(ref)
            if path.stat().st_size > 4 * 1024 * 1024:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["expires"] > time.time() and record["catalog"]["ref"] == ref:
                return record["catalog"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def _save_catalog_snapshot(self, ref, result):
        path = self._snapshot_path(ref)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"expires": time.time() + 600, "catalog": result}), encoding="utf-8")
            temporary.replace(path)
            # Retain at most 16 small version snapshots across reloads.
            files = sorted(path.parent.glob("*.json"), key=lambda entry: entry.stat().st_mtime, reverse=True)
            for old in files[16:]:
                if old.resolve().is_relative_to(path.parent.resolve()):
                    old.unlink(missing_ok=True)
        except OSError:
            pass  # A read-only cache must not prevent browsing.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _catalog_api(self, ref):
        sha = self._api("/commits/" + quote(ref, safe=""))["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("素材版本编号无效")
        rows = self._api("/contents/" + ART_PATH + "?ref=" + sha)
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise ValueError("素材目录不完整，请选择其他版本")
        directories = [row for row in rows if row.get("type") == "dir"]
        if len(directories) > 24:
            raise ValueError("素材目录过多，暂不支持该版本")
        def subtree(directory):
            tree_sha = directory.get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
                raise ValueError("素材子目录编号无效")
            tree = self._api("/git/trees/" + tree_sha + "?recursive=1")
            if tree.get("truncated"):
                raise ValueError("素材子目录不完整，请选择其他版本")
            return [{"type": "file", "name": row["path"].rsplit("/", 1)[-1],
                     "path": directory["path"] + "/" + row["path"]}
                    for row in tree.get("tree", []) if row.get("type") == "blob"]
        with ThreadPoolExecutor(max_workers=3) as executor:
            for children in executor.map(subtree, directories):
                rows.extend(children)
        return self._format_catalog(ref, sha, rows)

    @staticmethod
    def _format_catalog(ref, sha, rows):
        items = []
        for row in rows:
            name, relative = row.get("name", ""), row.get("path", "")
            if not relative.startswith(ART_PATH + "/") or ".." in relative.split("/"):
                continue
            if row.get("type") == "file" and name.lower().endswith(".png"):
                lower = relative.lower()
                if "/weapon/" in lower or "weapon" in name.lower() or re.match(r"T_Luckdraw\d+_UI", name):
                    kind = "weapon"
                elif "/role/" in lower or "role" in name.lower() or re.match(r"T_Luckdraw_[A-Za-z]+_UI", name):
                    kind = "character"
                else:
                    kind = "other"
                items.append({"name": name, "path": relative, "type": kind,
                              "portrait_url": f"https://raw.githubusercontent.com/{REPOSITORY}/{sha}/{quote(relative, safe='/')}"})
        return {"ref": ref, "commit": sha, "items": sorted(items, key=lambda row: row["name"])}

    def _page_payload(self, path):
        def load():
            raw = self._download(f"https://github.com/{REPOSITORY}{path}", max_bytes=4 * 1024 * 1024)
            for script in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', raw.decode("utf-8"), re.S):
                data = json.loads(script)
                if isinstance(data, dict) and "payload" in data:
                    return data["payload"]
            raise ValueError("GitHub 公开目录格式已变化，请稍后重试")
        return self._cached("page:" + path, load)

    def _catalog_html(self, ref):
        def tree(version, path):
            data = self._page_payload("/tree/" + quote(version, safe="") + "/" + quote(path, safe="/"))["codeViewTreeRoute"]
            rows = data["tree"]["items"]
            if len(rows) >= 1000 or data["tree"].get("totalCount", len(rows)) != len(rows):
                raise ValueError("GitHub 公开目录不完整，请待 API 配额恢复后重试")
            return data["refInfo"]["currentOid"], rows
        sha, entries = tree(ref, ART_PATH)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("素材版本编号无效")
        pending, rows, visited = [], [], 0
        while True:
            for row in entries:
                path = row.get("path", "")
                if not path.startswith(ART_PATH + "/") or ".." in path.split("/"):
                    continue
                if row.get("contentType") == "directory":
                    pending.append(path)
                elif row.get("contentType") == "file":
                    rows.append(dict(row, type="file"))
            if not pending:
                break
            batch, pending = pending[:3], pending[3:]
            visited += len(batch)
            if visited > 64:
                raise ValueError("素材目录过多，请待 API 配额恢复后重试")
            with ThreadPoolExecutor(max_workers=3) as executor:
                groups = list(executor.map(lambda path: tree(sha, path), batch))
            if any(commit != sha for commit, _ in groups):
                raise ValueError("素材目录版本不一致，请重试")
            entries = [row for _, group in groups for row in group]
        result = self._format_catalog(ref, sha, rows)
        result["source"] = "github-page"
        return result

    def preview(self, source):
        if self.store is not None:
            return self.store.preview(source)
        if not isinstance(source, str) or len(source) > 2048 or not source:
            raise ValueError("立绘地址无效")
        # Legacy CSVs wrap this same repository in public mirrors. Preview the
        # original HTTPS asset; never forward credentials or contact the mirror.
        parsed = urlsplit(source)
        if parsed.netloc in {"v6.gh-proxy.org", "gh-proxy.com"}:
            original = parsed.path.lstrip("/")
            if original.startswith(f"https://raw.githubusercontent.com/{REPOSITORY}/"):
                source = original
        def build():
            if source.startswith("local:"):
                path = managed_path(self.root, source[6:])
                if path.suffix.lower() not in {".png", ".webp"} or path.stat().st_size > 10 * 1024 * 1024:
                    raise ValueError("本地立绘格式或大小无效")
                raw = path.read_bytes()
            else:
                raw = self._download(source)
            if not raw:
                raise ValueError("立绘加载失败，请检查地址或网络")
            try:
                with Image.open(io.BytesIO(raw)) as original:
                    if original.width * original.height > 20_000_000:
                        raise ValueError("立绘尺寸过大")
                    if original.format not in {"PNG", "JPEG", "WEBP"}:
                        raise ValueError("不支持的立绘格式")
                    picture = original.convert("RGBA")
                    bounds = picture.getbbox()
                    if bounds:
                        picture = picture.crop(bounds)
                    picture.thumbnail((360, 360))
                    output = io.BytesIO()
                    picture.save(output, format="WEBP", quality=82)
                return {"data_url": "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode("ascii")}
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError("无法解码立绘图片") from exc
        return self._cached("preview:" + source, build, ttl=1800)

    def import_picture(self, group: str, source: str):
        """Save a selected remote portrait as a durable managed image.

        Rendering then reads local storage and never needs the upstream host.
        The original URL is only a content key and never becomes a path.
        """
        if not isinstance(source, str) or len(source) > 2048 or not source.startswith(("https://", "http://")):
            raise ValueError("请选择有效的远程立绘")
        key = hashlib.sha256(source.encode("utf-8")).hexdigest()
        relative = f"{group}/{key}.webp"
        target = managed_path(self.root, relative)
        if target.is_file():
            return {"portrait_url": "local:" + relative}
        legacy = managed_path(self.root, f"{group}/{key}.png")
        if legacy.is_file():
            return {"portrait_url": f"local:{group}/{key}.png"}
        parsed = urlsplit(source)
        if parsed.netloc in {"v6.gh-proxy.org", "gh-proxy.com"}:
            original = parsed.path.lstrip("/")
            if original.startswith(f"https://raw.githubusercontent.com/{REPOSITORY}/"):
                source = original
        raw = self.store.ensure(source) if self.store is not None else self._download(source)
        if not raw:
            raise ValueError("立绘下载失败，无法保存到本地")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                compact = raw if image.format == "WEBP" and max(image.size) <= 1280 else encode_portrait(raw)
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError("无法解码立绘图片") from exc
        if len(compact) > 16 * 1024 * 1024:
            raise ValueError("立绘转换后超过 16 MiB，请上传较小图片")
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(compact)
            temporary.replace(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return {"portrait_url": "local:" + relative}
