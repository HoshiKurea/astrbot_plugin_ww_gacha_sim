"""Exercise command matching and replies through a real AstrBot pipeline.

Run with AstrBot installed. ASTRBOT_SOURCE_ROOT optionally selects a checkout.
All plugin data and host state used here are isolated from the running bot.
"""

import asyncio
import base64
import functools
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if source_root := os.environ.get("ASTRBOT_SOURCE_ROOT"):
        sys.path.insert(0, source_root)

    # Set the host root before importing AstrBot: importing older releases can
    # create/migrate their default dashboard configuration.
    host_directory = tempfile.TemporaryDirectory(prefix="ww_command_host_")
    previous_root = os.environ.get("ASTRBOT_ROOT")
    os.environ["ASTRBOT_ROOT"] = host_directory.name

    from astrbot import __version__
    from astrbot.api.message_components import Plain
    from astrbot.api.star import StarTools
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.filter.event_message_type import EventMessageTypeFilter, EventMessageType
    from astrbot.core.star.star import StarMetadata, star_map
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry
    from astrbot_plugin_ww_gacha_sim.main import WutheringWavesGachaPlugin
    from astrbot_plugin_ww_gacha_sim.src.services.render_service import RenderService
    from PIL import Image as PillowImage

    class TestEvent(AstrMessageEvent):
        def __init__(self, text):
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.message_str = text
            message.message = [Plain(text)]
            message.sender = MessageMember("command-test", "Command Test")
            message.self_id = "bot"
            message.session_id = "command-test"
            message.message_id = "command-test"
            super().__init__(text, message, PlatformMetadata("webchat", "test", "test"),
                             "command-test")
            self.sent = []
            self.sent_chains = []
            self.sent_at = []

        async def send(self, result):
            self.sent.append(result.get_plain_text())
            self.sent_chains.append(list(result.chain))
            self.sent_at.append(asyncio.get_running_loop().time())
            self._has_send_oper = True

    class Respond:
        async def process(self, event):
            if result := event.get_result():
                if result.chain:
                    await event.send(result)
                event.clear_result()

    async def default_chat(event):
        event.set_extra("default_chat_called", True)
        event.set_result(event.plain_result("DEFAULT_CHAT"))
        yield

    async def broad_chat(event):
        yield event.plain_result("OTHER_PLUGIN_CHAT")

    original_data_dir = StarTools.__dict__["get_data_dir"]
    with tempfile.TemporaryDirectory(prefix="ww_command_test_") as directory:
        StarTools.get_data_dir = classmethod(lambda cls, plugin_name=None: Path(directory))
        plugin = WutheringWavesGachaPlugin(
            SimpleNamespace(register_web_api=lambda *args: None), {"enable_rendering": False}
        )
        module = plugin.__module__
        old_metadata = star_map.get(module)
        star_map[module] = StarMetadata(name="astrbot_plugin_ww_gacha_sim", module_path=module,
                                       star_cls=plugin)
        handlers = star_handlers_registry.get_handlers_by_module_name(module)
        raw_handlers = [(handler, handler.handler) for handler in handlers]
        for handler, raw in raw_handlers:
            handler.handler = functools.partial(raw, plugin)
        chat_module = "ww_command_test.chat"
        star_map[chat_module] = StarMetadata(name="test_chat", module_path=chat_module)
        chat_handler = StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name=f"{chat_module}_chat",
            handler_name="chat", handler_module_path=chat_module, handler=broad_chat,
            event_filters=[EventMessageTypeFilter(EventMessageType.ALL)],
            extras_configs={"priority": 0},
        )
        star_handlers_registry.append(chat_handler)
        try:
            await plugin.initialize()
            plugin.get_kv_data = AsyncMock(return_value=None)
            plugin.put_kv_data = AsyncMock()

            async def dispatch(text, prefixes=None, plugin_set=None, message_id=None):
                config = {
                    "wake_prefix": prefixes if prefixes is not None else ["/"],
                    "admins_id": [], "platform_settings": {},
                    "provider_settings": {"enable": True, "prompt_prefix": "", "identifier": False},
                    "plugin_set": plugin_set if plugin_set is not None else ["*"],
                }
                context = SimpleNamespace(astrbot_config=config, astrbot_config_id="test",
                                          db_helper=None)
                wake = WakingCheckStage()
                await wake.initialize(context)
                request = StarRequestSubStage()
                await request.initialize(context)
                process = ProcessStage()
                process.ctx = context
                process.star_request_sub_stage = request
                process.agent_sub_stage = SimpleNamespace(process=default_chat)
                # Use the actual scheduler's yield/resume and stop-event semantics.
                scheduler = object.__new__(PipelineScheduler)
                scheduler.stages = [wake, process, Respond()]
                event = TestEvent(text)
                if message_id is not None:
                    event.message_obj.message_id = message_id
                with patch("astrbot.core.star.session_plugin_manager.sp.get_async",
                           new=AsyncMock(return_value={})):
                    await scheduler.execute(event)
                return event

            for text, expected in (
                ("/wwg 帮助", "鸣潮模拟抽卡帮助"),
                ("/wwg_help", "鸣潮模拟抽卡帮助"),
                ("/wgs_help", "鸣潮模拟抽卡帮助"),
                ("/鸣潮帮助", "鸣潮模拟抽卡帮助"),
                ("/ww抽卡 帮助", "鸣潮模拟抽卡帮助"),
                ("/鸣潮 帮助", "鸣潮模拟抽卡帮助"),
                ("/wwg 卡池", "当前可用的卡池"),
                ("/ww抽卡 卡池", "当前可用的卡池"),
                ("/鸣潮 卡池", "当前可用的卡池"),
                ("/wwg 选择", "请先发送 /wwg 卡池"),
                ("/wwg 单抽", "单次抽卡结果"),
                ("/wwg 十连", "十连抽卡结果"),
                ("/wwg 保底", "本组累计抽数"),
                ("/wwg 记录", "抽卡记录"),
                ("/wwg 单抽 missing-pool", "找不到"),
                ("/单抽 missing-pool", "找不到"),
                ("/wwg", "wwg 指令组"),
                ("/鸣潮", "指令组"),
                ("/ww抽卡", "指令组"),
                ("/wwg 诊断", "权限不足"),
                ("/鸣潮 诊断", "权限不足"),
            ):
                event = await dispatch(text)
                assert event.sent and expected in event.sent[0], (text, event.sent)
                assert len(event.sent) == 1, (text, event.sent)
                assert all("CHAT" not in reply for reply in event.sent), (text, event.sent)
                assert not event.get_extra("default_chat_called"), text
                assert event.is_stopped(), text

            for text in ("!wwg 帮助", "!wwg_help", "!wgs_help"):
                event = await dispatch(text, ["!"])
                assert "主入口：/wwg" in event.sent[0] and len(event.sent) == 1
            # Honor host prefixes and deliberately disabled plugins/commands.
            for prefixes in ([""], ["", "/"], ["!"]):
                event = await dispatch("/wwg 帮助", prefixes)
                assert all("鸣潮模拟抽卡帮助" not in reply for reply in event.sent)
            for text in ("/wwg 帮助", "/wwg_help", "/wgs_help"):
                event = await dispatch(text, plugin_set=["test_chat"])
                assert event.sent == ["OTHER_PLUGIN_CHAT"]
            help_handler = next(handler for handler in handlers if handler.handler_name == "wgs_help")
            help_handler.enabled = False
            try:
                for text in ("/wwg_help", "/wgs_help"):
                    event = await dispatch(text)
                    assert event.sent == ["OTHER_PLUGIN_CHAT"]
            finally:
                help_handler.enabled = True
            event = await dispatch("普通对话")
            assert event.sent == ["OTHER_PLUGIN_CHAT"]
            chat_handler.enabled = False
            try:
                event = await dispatch("普通对话")
                assert event.sent == ["DEFAULT_CHAT"]
            finally:
                chat_handler.enabled = True

            # A cold draw must deliver text before resource preparation, then
            # an actual image through the same live pipeline; downloads may
            # take longer than the separate CPU rendering budget.
            async def prepare(*args, **kwargs):
                await asyncio.sleep(0.6)

            plugin.portrait_service = SimpleNamespace(
                missing=lambda items: items, prepare=prepare, close=AsyncMock()
            )
            plugin.render_service = RenderService(workers=1, timeout_seconds=0.5)
            plugin.renderer = SimpleNamespace(render_ten_pulls=lambda *args, **kwargs:
                                               PillowImage.new("RGB", (16, 16), "teal"))
            plugin.enable_rendering = True
            count_before = await plugin.gacha_service.history_count("test", "command-test", None)
            cold = await dispatch("/wwg 十连", message_id="cold-render-test")
            assert len(cold.sent) == 2 and "十连抽卡结果" in cold.sent[0]
            assert "补发完整图片" in cold.sent[0] and cold.sent[1] == ""
            assert cold.sent_at[1] - cold.sent_at[0] >= 0.55
            image = cold.sent_chains[1][0]
            assert base64.b64decode(image.file.removeprefix("base64://")).startswith(b"\xff\xd8")
            assert await plugin.gacha_service.history_count("test", "command-test", None) == count_before + 10
            assert cold.is_stopped() and not cold.get_extra("default_chat_called")
            print(f"AstrBot {__version__}: commands, aliases, parameters, reply delivery, "
                  "chat isolation, configured prefixes, disabled scopes, cold text/image replies OK")
        finally:
            await plugin.terminate()
            for handler, raw in raw_handlers:
                handler.handler = raw
            star_handlers_registry.remove(chat_handler)
            star_map.pop(chat_module, None)
            if old_metadata is not None:
                star_map[module] = old_metadata
            else:
                star_map.pop(module, None)
            StarTools.get_data_dir = original_data_dir
            if previous_root is None:
                os.environ.pop("ASTRBOT_ROOT", None)
            else:
                os.environ["ASTRBOT_ROOT"] = previous_root
            host_directory.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
