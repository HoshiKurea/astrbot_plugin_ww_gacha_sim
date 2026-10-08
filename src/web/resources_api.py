"""Short-lived HTTP requests over owned background artwork jobs."""

from astrbot.api.web import error_response, json_response

from ..services.resource_service import MAX_PACKAGE_BYTES
from ..services.work_queue import WorkBusy


class ResourceAPI:
    def _resource_guard(self):
        denied = self._guard()
        if denied is not None:
            return denied
        if self.resources is None:
            return error_response("资源服务不可用，请重载插件", status_code=503)
        return None

    async def resource_status(self):
        if denied := self._resource_guard():
            return denied
        request = self._request()
        job_id, pool_id = request.query.get("job_id", ""), request.query.get("pool_id", "")
        try:
            if job_id:
                return json_response({"job": self.resources.snapshot(job_id)})
            result = await self.database.run(self.resources.describe, pool_id)
            return json_response(dict(result, job=self.resources.latest(pool_id),
                                      import_job=self.resources.latest("")))
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def prepare_resources(self):
        if denied := self._resource_guard():
            return denied
        try:
            payload = await self._payload()
            return json_response({"job": await self.resources.start(payload.get("pool_id"))})
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def cancel_resources(self):
        if denied := self._resource_guard():
            return denied
        try:
            payload = await self._payload()
            return json_response({"job": self.resources.cancel(payload.get("job_id"))})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def export_resources(self):
        if denied := self._resource_guard():
            return denied
        try:
            from astrbot.api.web import file_response
            path = await self.resources.export(self._request().query.get("pool_id", ""))
            return file_response(path, filename="wwg-offline-resources.zip", content_type="application/zip")
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, OSError) as exc:
            return error_response(str(exc), status_code=400)

    async def import_resources(self):
        if denied := self._resource_guard():
            return denied
        job = None
        try:
            # Reserve admission before reading a possibly large upload.
            job = self.resources.reserve("import")
            upload = (await self._request().files()).get("file")
            if not self._is_upload(upload):
                raise ValueError("请选择本插件导出的离线 ZIP")
            if upload.content_length and upload.content_length > MAX_PACKAGE_BYTES:
                raise ValueError("离线资源包超过 64 MiB")
            raw = await upload.read(MAX_PACKAGE_BYTES + 1)
            if len(raw) > MAX_PACKAGE_BYTES:
                raise ValueError("离线资源包超过 64 MiB")
            return json_response({"job": self.resources.start_import(raw, job)})
        except WorkBusy as exc:
            return error_response(str(exc), status_code=429)
        except (ValueError, OSError) as exc:
            return error_response(str(exc), status_code=400)
        finally:
            if job is not None and job["id"] not in self.resources.tasks:
                job["state"] = "failed"
