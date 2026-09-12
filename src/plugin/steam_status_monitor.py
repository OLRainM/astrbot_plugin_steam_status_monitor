from astrbot.api.star import Star, Context
from ..shared.logging import logger
from ..shared.network import httpx_client_kwargs, requests_verify
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image  # 确保已导入 Image
import base64
import time
import httpx
import asyncio
import os
import random
from ..application.services.openbox import handle_openbox
from ..application.services.steam_list import handle_steam_list, build_player_row
from ..application.services.monitor_admin import MonitorAdminService
from ..application.services.ranking import RankingService
from ..application.services.price_query import PriceQueryService
import re
from ..application.services.achievement_monitor import AchievementMonitor
from ..application.services.achievement_tracking import AchievementTrackingMixin
from ..application.services.notification_tracking import NotificationTrackingMixin
from ..application.services.session_quit import SessionQuitMixin
from ..application.services.status_change_tracking import StatusChangeTrackingMixin
from ..application.services.polling_tracking import PollingTrackingMixin
from ..presentation.renderers.game_start import render_game_start
from ..presentation.renderers.game_end import render_game_end
from ..presentation.renderers.rank import render_rank_image
from ..presentation.renderers.game_detail import render_game_detail_image
from ..domain.monitoring import MonitorStateStore, StateBackedMonitorMixin
from ..shared.fonts import resolve_font_path
from ..domain.ranking.push_scopes import build_rank_push_scopes
from PIL import Image as PILImage
import io
from datetime import date
import requests  # 新增导入
import tempfile
import traceback
import shutil
from ..presentation.web.admin_api import WebAdminAPI
from ..infrastructure.persistence.plugin_data import PersistenceMixin
from ..infrastructure.fonts import FontPackService
from ..infrastructure.clients.steam import SteamClientMixin
from ..application.services.qq_menu_management import QQMenuManagementMixin
from ..shared.paths import ABILITIES_PATH
from ..shared.utils.price import extract_price_query, extract_steam_appid
from ..shared.utils.notify_session import is_sendable_group_session, is_valid_group_id
from .runtime_config import apply_hot_update, apply_runtime_config


class SteamStatusMonitorV3(
    QQMenuManagementMixin,
    PollingTrackingMixin,
    StatusChangeTrackingMixin,
    SessionQuitMixin,
    NotificationTrackingMixin,
    AchievementTrackingMixin,
    StateBackedMonitorMixin,
    PersistenceMixin,
    SteamClientMixin,
    Star,
):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.monitor_state = MonitorStateStore()
        # 插件运行状态标志，重启后自动丢失
        if hasattr(self, '_ssm_running') and self._ssm_running:
            logger.error("当前插件已在运行中。请重启astrbot而非重载插件")
            return
        self._ssm_running = True
        self._plugin_version = "4.7.3"
        self.context = context
        # 分群管理：所有状态数据均以 group_id 为 key
        self.group_steam_ids = {}         # {group_id: [steamid, ...]}
        self.group_last_states = {}       # {group_id: {steamid: status}}
        self.group_last_quit_times = {}   # {group_id: {steamid: {gameid: quit_time}}}
        self.group_pending_logs = {}      # {group_id: {steamid: {gameid: log_dict}}}
        self.group_recent_games = {}      # {group_id: [gameid, ...]}
        self._session_meta = {}           # {(group_id, sid): {player_name, game_name, avatar_url}}
        # 超能力缓存和能力列表
        self._superpower_cache = {}  # {(steamid, date): superpower}
        self._abilities = None
        self._abilities_path = str(ABILITIES_PATH)
        self._game_name_cache = {}  # 修复: 游戏名缓存，防止 AttributeError
        apply_runtime_config(self, config)
        self._steam_search_cache = {}
        self._steam_search_pending = {}
        self.next_poll_time = {}  # {group_id: {steamid: next_time}}
        # 数据持久化目录
        self.data_dir = os.path.join("data", "steam_status_monitor")
        os.makedirs(self.data_dir, exist_ok=True)
        self.font_pack = FontPackService(
            self.data_dir,
            proxy=self.proxy,
            enabled=bool(self.config.get("font_download_enabled", True)),
            pack_url=str(self.config.get("font_pack_url", "") or ""),
            timeout_sec=int(self.config.get("font_download_timeout_sec", 600) or 600),
        )
        self._font_pack_task = self.font_pack.ensure_ready()
        self._load_group_steam_ids()  # 新增：优先从 steam_groups.json 加载
        self._load_persistent_data()
        self._load_notify_session()
        # 成就监控
        self.achievement_monitor = AchievementMonitor(self.data_dir, steam_api_base=self.STEAM_API_BASE, proxy=self.proxy)
        # 首次启动：校验历史成就黑名单，自动移出被误拉黑（本身有成就）的游戏；仅执行一次
        if not self.achievement_monitor.is_blacklist_verified():
            self._achievement_blacklist_verify_task = asyncio.create_task(
                self.achievement_monitor.verify_blacklist_once()
            )
        self.achievement_poll_tasks = {}  # {(group_id, sid, gameid): asyncio.Task}
        self.achievement_snapshots = {}   # {(group_id, sid, gameid): [成就列表]}
        self.achievement_blacklist = set()  # 新增：成就查询黑名单
        self.achievement_fail_count = {}    # 新增：成就查询失败计数
        # --- 新增：重启后自动推送 ---
        self.running_groups = set()  # 正在运行的群号集合
        self.group_monitor_enabled = {}      # {group_id: bool} 监控开关
        self.group_achievement_enabled = {}  # {group_id: bool} 成就推送开关
        self._load_group_switches()
        self._qq_menu_lock = asyncio.Lock()
        self._platform_id = None  # 记录消息平台ID，用于WebUI自动补全通知目标
        # --- WebUI 群自动补全 notify_sessions ---
        self._auto_fill_notify_sessions()
        # --- 新增：重启后自动恢复所有群的轮询 ---
        if hasattr(self, 'notify_sessions') and self.notify_sessions and self.API_KEY and self.group_steam_ids:
            logger.info(f"[SteamStatusMonitor] 检测到 notify_sessions={self.notify_sessions}，自动启动监控轮询")
            for group_id in self.notify_sessions:
                if group_id in self.group_steam_ids and self.group_monitor_enabled.get(group_id, True):
                    self.running_groups.add(group_id)
        # --- 新增：全局日志收集与统一输出 ---
        self._last_round_logs = []  # [(group_id, logstr)]
        # --- 新增：持久化数据脏标志 + 节流保存，避免高频写盘拖慢主循环 ---
        self._data_dirty = False          # 有变更待保存
        self._last_save_time = time.time() # 上次保存时间戳
        self._save_interval = 300          # 节流间隔（秒），300秒=5分钟
        # --- 插件启动时间戳 + 启动初始化期间的"陈旧群"标记（init 完成后清空） ---
        self._startup_time = time.time()
        self._startup_stale_groups = {}
        # 保存任务引用，便于 terminate 时取消，防止重载/禁用后残留多实例并发
        self._poll_loop_task = asyncio.create_task(self.global_poll_and_log_loop())
        self._init_poll_task = asyncio.create_task(self.init_poll_time_once())
        self._load_push_groups()  # <--- 修复：确保push_groups属性初始化
        # --- 排行榜功能：游玩时长记录 + 去重缓存 + 每日推送开关 ---
        self.play_records = {}              # {date_str: {steamid: {gameid: {name, minutes}}}}
        self.session_records = {}           # {steamid: [session_dict]} 甘特图/热力图数据
        self._session_dirty = False         # session 数据脏标志
        self._recorded_quit_cache = {}      # {(steamid, gameid): timestamp} 去重用
        self.ranking_service = RankingService(self)
        self.price_query = PriceQueryService(self, translator=self._translate_game_query)
        self.rank_push_groups = []          # 开启了每日排行榜推送的群列表
        self.rank_push_all = False           # True=全群统一推送全局排行（只渲染一次）
        self._last_rank_push_date = None    # 记录上次推送日期，防止同一天重复推送
        self._load_play_records()
        self._load_session_records()
        self._load_rank_push_groups()
        # QQ-SteamID 绑定数据
        self._bind_data = {}  # {qq: {sid, nickname}}
        self._load_bind_data()
        # --- 通知合并缓冲区：SessionService 将开始/结束通知写入此队列，由主轮询统一 flush ---
        self._pending_end_notifications = {}  # {group_id: [notification_dict, ...]}
        # --- AstrBot Plugin Pages 管理后台 ---
        self.web_api = WebAdminAPI(self)
        self.web_api.register_routes(context)
        logger.info("[WebAdmin] 管理页面已注册到 AstrBot 内置 WebUI")

    async def terminate(self):
        '''插件被卸载/停用时取消所有后台任务并保存持久化数据'''
        # 取消主轮询循环和初始化任务，防止重载/禁用后残留多实例并发
        for t in (getattr(self, '_poll_loop_task', None), getattr(self, '_init_poll_task', None), getattr(self, '_font_pack_task', None)):
            if t and not t.done():
                t.cancel()
        font_pack = getattr(self, 'font_pack', None)
        if font_pack:
            await font_pack.aclose()
        if hasattr(self, 'achievement_poll_tasks'):
            for task in self.achievement_poll_tasks.values():
                task.cancel()
            self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        # 保存持久化数据（强制落盘，不节流）
        self._save_persistent_data(force=True)
        # 重置运行标志，允许下次重载正常初始化
        self._ssm_running = False

    def crop_image_auto(self, img_path_or_bytes, bg_color=(20,26,33), threshold=25):
        """
        自动裁剪图片内容区域，去除边缘与 bg_color 相近的空白。
        支持本地路径、bytes、URL、PIL.Image。
        """
        import numpy as np
        # 新增：如果已经是PIL.Image对象，直接用
        if isinstance(img_path_or_bytes, PILImage.Image):
            img = img_path_or_bytes.convert("RGB")
        elif isinstance(img_path_or_bytes, str) and (img_path_or_bytes.startswith("http://") or img_path_or_bytes.startswith("https://")):
            resp = requests.get(img_path_or_bytes, timeout=15, verify=requests_verify())
            resp.raise_for_status()
            img = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
        elif isinstance(img_path_or_bytes, bytes):
            img = PILImage.open(io.BytesIO(img_path_or_bytes)).convert("RGB")
        else:
            img = PILImage.open(img_path_or_bytes).convert("RGB")
        arr = np.array(img)
        # 自动检测背景色（取四角平均色）
        h, w, _ = arr.shape
        corners = [arr[0,0], arr[0,-1], arr[-1,0], arr[-1,-1]]
        avg_bg = np.mean(corners, axis=0)
        # 计算每个像素与背景色的距离
        diff = np.abs(arr - avg_bg).sum(axis=2)
        mask = diff > threshold
        coords = np.argwhere(mask)
        if coords.size == 0:
            return img
        y0, x0 = coords.min(axis=0)
        y1, x1 = coords.max(axis=0) + 1
        # 防止裁剪过度，留出2px边距
        y0 = max(y0 - 0, 0)
        x0 = max(x0 - 0, 0)
        y1 = min(y1 - 0, arr.shape[0])
        x1 = min(x1 - 0, arr.shape[1])
        cropped = img.crop((x0, y0, x1, y1))
        return cropped


    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam on")
    async def steam_on(self, event: AstrMessageEvent):
        '''手动启动Steam状态监控轮询（分群）'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        if not is_valid_group_id(group_id):
            yield event.plain_result("请在群聊中使用该命令，私聊无法启动群监控。")
            return
        self.group_monitor_enabled[group_id] = True
        self._save_group_switches()
        if not self.API_KEY:
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        steam_ids = self.group_steam_ids.get(group_id, [])
        if not steam_ids or not any(isinstance(x, str) and x.strip() for x in steam_ids):
            yield event.plain_result(
                "未设置监控的 SteamID 列表，请先在插件配置中填写 steam_ids，"
                "或使用 /steam addid [SteamID] 添加要监控的玩家。"
            )
            return
        if group_id in self.running_groups:
            yield event.plain_result("本群Steam监控已在运行。")
            return
        self.running_groups.add(group_id)
        if not hasattr(self, 'notify_sessions'):
            self.notify_sessions = {}
        self.notify_sessions[group_id] = event.unified_msg_origin
        self._record_platform_id(event)
        self._save_notify_session()
        # 初始化状态
        if group_id not in self.group_last_states:
            self.group_last_states[group_id] = {}
        # 批量查询所有玩家状态，减少API调用
        status_map = await self.fetch_player_statuses_batch(steam_ids) if steam_ids else {}
        now = int(time.time())
        for sid in steam_ids:
            status = status_map.get(sid)
            if not status:
                continue
            self.group_last_states[group_id][sid] = status
            await self.session_service.handle(
                group_id,
                sid,
                status.get('gameid'),
                now,
                player_name=status.get('name') or sid,
                current_game_name=status.get('gameextrainfo') or '未知游戏',
                status=status,
                skip_push=True,
            )
        yield event.plain_result("本群Steam状态监控启动完成喔！ヾ(≧ω≦)ゞ")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam addid")
    async def steam_addid(self, event: AstrMessageEvent, steamid: str, at_user: str = "", nickname: str = ""):
        '''添加SteamID到本群监控列表（分群），支持逗号分隔多个ID，支持SteamID/个人资料链接/自定义ID/好友码
        末尾可加 @用户 [备注名] 绑定QQ与SteamID'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        if not is_valid_group_id(group_id):
            yield event.plain_result("请在群聊中使用该命令，或到 WebUI 填写有效群号后再添加。")
            return
        bind_qq = None
        bind_nickname = None
        if at_user:
            m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', at_user.strip())
            bind_qq = (m.group(1) or m.group(2) or m.group(3) or m.group(4)) if m else None
        if nickname:
            bind_nickname = nickname.strip()
        raw_list = [x.strip() for x in re.split(r'[,，]+', steamid) if x.strip()]
        resolved_list = []
        invalid_list = []
        for raw in raw_list:
            sid = await self.resolve_steam_input(raw)
            if sid and sid.isdigit() and len(sid) == 17:
                resolved_list.append(sid)
            else:
                invalid_list.append(raw)
        if invalid_list:
            yield event.plain_result(
                f"以下输入无法解析为有效SteamID：{', '.join(invalid_list)}\n"
                f"支持格式：17位SteamID64 / 个人资料链接 / 自定义ID链接 / 8位好友码"
            )
            return
        seen = set()
        steamid_list = []
        for sid in resolved_list:
            if sid not in seen:
                seen.add(sid)
                steamid_list.append(sid)
        admin = MonitorAdminService(self)
        added = []
        pushed = []
        already = []
        already_pushed = []
        binding_updated = []
        pushed_primary_groups = {}
        limit = self.max_group_size
        for sid in steamid_list:
            result = admin.add_player(group_id, sid)
            if result.message == "already exists":
                already.append(sid)
                if bind_qq or bind_nickname:
                    binding_updated.append(sid)
                continue
            if result.message == "already push group":
                already_pushed.append(sid)
                pushed_primary_groups[sid] = admin.primary_group_of(sid)
                continue
            if result.message == "added as push group":
                pushed.append(sid)
                pushed_primary_groups[sid] = admin.primary_group_of(sid)
                continue
            if result.message == "added as primary monitor":
                added.append(sid)
                continue
            if "group limit reached" in result.message:
                break
        if steamid_list and (bind_qq or bind_nickname):
            for sid in steamid_list:
                admin.bind_player(sid, qq=bind_qq, nickname=bind_nickname)
            logger.info(f"[绑定] {'QQ'+str(bind_qq) if bind_qq else '备注'} -> SteamID {steamid_list[-1]}，备注={bind_nickname or '无'}")
        msg = ""
        if added:
            msg += f"已为本群添加SteamID: {', '.join(added)}\n"
        if pushed:
            push_details = []
            for sid in pushed:
                primary_group = pushed_primary_groups.get(sid)
                suffix = f"（主监控群：{primary_group}）" if primary_group else ""
                push_details.append(f"{sid}{suffix}")
            msg += (
                "以下SteamID已被其他群监控，当前群不会重复监控，已自动设置为分发路由（push_group）："
                f"{', '.join(push_details)}\n"
            )
        if binding_updated:
            msg += f"以下SteamID已在本群监控，备注/绑定已更新：{', '.join(binding_updated)}\n"
        already_plain = [sid for sid in already if sid not in binding_updated]
        if already_plain:
            msg += f"以下SteamID已经在本群监控，无需重复添加：{', '.join(already_plain)}\n"
        if already_pushed:
            push_details = []
            for sid in already_pushed:
                primary_group = pushed_primary_groups.get(sid)
                suffix = f"（主监控群：{primary_group}）" if primary_group else ""
                push_details.append(f"{sid}{suffix}")
            msg += f"以下SteamID已经是本群的分发路由（push_group），无需重复添加：{', '.join(push_details)}\n"
        unhandled = len(steamid_list) - len(added) - len(pushed) - len(already) - len(already_pushed)
        if unhandled:
            msg += f"本群监控组人数已达上限（{limit}人），部分ID未添加。\n"
        # 自动启用本群监控（幂等）
        if added and group_id not in self.running_groups:
            self.group_monitor_enabled[group_id] = True
            self.running_groups.add(group_id)
            if not hasattr(self, 'notify_sessions'):
                self.notify_sessions = {}
            self.notify_sessions[group_id] = event.unified_msg_origin
            self._record_platform_id(event)
            self._save_notify_session()
            if group_id not in self.group_last_states:
                self.group_last_states[group_id] = {}
            msg += "监控已自动启动。\n"
        yield event.plain_result(msg.strip() if msg else "未添加任何SteamID。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam delid")
    async def steam_delid(self, event: AstrMessageEvent, steamid: str, group_id_param: str = ""):
        '''从监控组删除SteamID；支持好友码/链接；可选传群号跨群删除：/steam delid [SteamID/好友码/链接] [群号]'''
        group_id = group_id_param.strip() if group_id_param.strip() else (str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default')
        # 支持好友码/链接解析为64位ID
        sid = await self.resolve_steam_input(steamid)
        if not sid or not sid.isdigit() or len(sid) != 17:
            yield event.plain_result("无法解析为有效SteamID，支持格式：17位SteamID64 / 个人资料链接 / 8位好友码")
            return
        from ..application.services.monitor_admin import MonitorAdminService

        result = MonitorAdminService(self).remove_player(group_id, sid)
        if not result.changed:
            yield event.plain_result(
                f"该SteamID不存在于群 {group_id} 的监控组或分发路由: {sid}"
            )
            return

        if result.message == "removed push route":
            yield event.plain_result(f"已关闭群 {group_id} 对 SteamID {sid} 的分发路由")
        else:
            yield event.plain_result(
                f"已删除 SteamID {sid} 的主监控及全部路由分发"
            )

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam game")
    async def steam_game(self, event: AstrMessageEvent, appid: str):
        """查询 Steam 游戏详情并生成详情卡片。"""
        appid = str(appid).strip()
        if not appid.isdigit():
            yield event.plain_result("用法：/steam game <Steam AppID>")
            return
        card = await self.price_query.build_store_card(appid)
        if not card or not card.detail:
            yield event.plain_result(f"未找到 Steam 游戏 AppID：{appid}，或 Steam 商店暂时无法访问。")
            return
        try:
            img_bytes = await render_game_detail_image(
                card.card_data,
                font_path=resolve_font_path("NotoSansHans-Regular.otf"),
                proxy=self.proxy,
            )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                image_path = tmp.name
            yield event.image_result(image_path)
        except Exception as exc:
            logger.exception("渲染 Steam 游戏详情卡片失败: %s", exc)
            yield event.plain_result(f"游戏详情获取成功，但卡片生成失败：{exc}")

    @staticmethod
    def _steam_search_session_key(event: AstrMessageEvent) -> str:
        """Return a stable key for both group and private conversations."""
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if origin:
            return origin
        session_id = str(event.get_session_id() or "").strip()
        return session_id or "default"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def steam_price_selection(self, event: AstrMessageEvent):
        """接收群聊或私聊中的候选游戏序号。"""
        session_key = self._steam_search_session_key(event)
        if not self._steam_search_pending.get(session_key):
            return
        message = str(event.get_message_str() or "").strip()
        if message.startswith("/"):
            return
        match = re.search(r"([1-9]\d?)\s*$", message)
        if not match:
            return
        event.stop_event()
        async for result in self.steam_price(event, match.group(1)):
            yield result

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    async def _translate_game_query(self, query: str) -> str:
        """将中文游戏名转换为 Steam/ITAD 更容易命中的英文官方名。"""
        if not self._contains_chinese(query):
            return query
        try:
            provider = self.context.get_using_provider()
            if not provider:
                return query
            response = await provider.text_chat(
                prompt=(
                    "请将以下游戏名翻译为 Steam 商店使用的英文官方名称，"
                    f"仅输出英文名，不要输出其他内容：{query}"
                ),
                contexts=[],
                image_urls=[],
                func_tool=None,
                system_prompt="",
            )
            raw = str(response.completion_text or "").strip()
            # 剥离 LLM 推理/思考内容：取最后一个 </thinking>/</think> 之后的部分作为最终答案，
            # 前面的思考内容丢弃；若没有闭合标签则删除独立 think 标签，避免思考内容混入搜索结果。
            closes = list(re.finditer(r"</(?:thinking|think)[^>]*>", raw, flags=re.I))
            if closes:
                raw = raw[closes[-1].end():]
            else:
                raw = re.sub(r"<think[^>]*>", "", raw, flags=re.I)
                raw = re.sub(r"</?(?:thinking|think)[^>]*>", "", raw, flags=re.I)
            translated = re.sub(
                r"^(?:英文名|翻译结果|Translation)\s*[:：]?\s*",
                "",
                raw,
                flags=re.IGNORECASE,
            ).strip().strip('`\"“”')
            if translated:
                logger.info("[LLM][翻译游戏名] %s -> %s", query, translated)
                return translated
        except Exception as exc:
            logger.warning("LLM 翻译游戏名失败，将使用原始查询: %s", exc)
        return query

    async def _steam_price(self, event: AstrMessageEvent, auto_first: bool, prefix: str):
        """按中文名、英文名或 Steam 链接查询当前价格与历史最低价（px 为 auto_first 快捷版）。"""
        # 剥离命令前缀，保留含空格与数字的完整参数（fix 参数截断）。
        raw_msg = getattr(event, "message_str", None)
        if raw_msg is None:
            getter = getattr(event, "get_message_str", None)
            raw_msg = getter() if callable(getter) else ""
        query = extract_price_query(str(raw_msg or ""), prefix)
        if not query:
            yield event.plain_result(f"用法：/steam {prefix} <游戏名或 Steam 链接>")
            return
        session_key = self._steam_search_session_key(event)
        pending = self._steam_search_pending.get(session_key)
        selected_from_cache = False
        if not auto_first and query.isdigit() and pending:
            index = int(query) - 1
            games = self._steam_search_cache.get(session_key, [])
            if 0 <= index < len(games):
                game = games[index]
                selected_from_cache = True
            else:
                yield event.plain_result("候选序号无效，请重新回复序号。")
                return
        else:
            games = await self.price_query.resolve_games(query)
            if not games:
                if extract_steam_appid(query):
                    yield event.plain_result("未能通过该商店链接查到 ITAD 价格，请改用游戏名查询。")
                else:
                    yield event.plain_result("未找到匹配游戏，或 ITAD 暂时无法访问。")
                return
            game = games[0]
        if not auto_first and not selected_from_cache and len(games) > 1:
            self._steam_search_cache[session_key] = games
            self._steam_search_pending[session_key] = True
            lines = ["找到多个匹配游戏，请回复序号："]
            for index, game in enumerate(games, 1):
                lines.append(f"{index}. {game.title}")
            yield event.plain_result("\n".join(lines))
            return
        card = await self.price_query.build_card(game)
        self._steam_search_pending.pop(session_key, None)
        self._steam_search_cache.pop(session_key, None)
        try:
            img_bytes = await render_game_detail_image(
                card.card_data,
                font_path=resolve_font_path("NotoSansHans-Regular.otf"),
                proxy=self.proxy,
                itad_summary=card.summary,
                region_prices=card.region_prices,
            )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                image_path = tmp.name
            with open(image_path, "rb") as image_file:
                image_base64 = base64.b64encode(image_file.read()).decode("ascii")
            result = event.make_result().base64_image(image_base64)
            if card.store_message:
                result.message(card.store_message)
            yield result
            return
        except Exception as exc:
            logger.exception("渲染 Steam 价格详情卡片失败: %s", exc)

        if card.store_message:
            yield event.plain_result(card.store_message)
        else:
            yield event.plain_result("未找到对应的 Steam 商店链接。")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam price")
    async def steam_price(self, event: AstrMessageEvent, query: str):
        """价格查询（多个匹配时列出候选并等待回复序号）。"""
        async for result in self._steam_price(event, False, "price"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam px")
    async def steam_px(self, event: AstrMessageEvent, query: str):
        """价格查询快捷版（price 缩写）：无需回复序号，直接返回第一条匹配游戏的价格。"""
        async for result in self._steam_price(event, True, "px"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam list")
    async def steam_list(self, event: AstrMessageEvent):
        '''列出本群所有玩家当前状态（分群）'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        direct_steam_ids = self.group_steam_ids.get(group_id, [])
        push_steam_ids = [
            sid
            for sid, push_groups in (getattr(self, 'push_groups', {}) or {}).items()
            if group_id in {str(target) for target in push_groups}
        ]
        steam_ids = list(dict.fromkeys([*direct_steam_ids, *push_steam_ids]))
        if not self.API_KEY:
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        if not steam_ids:
            yield event.plain_result("本群未设置监控的 SteamID 列表，请先添加。"); return
        event.group_steam_ids = steam_ids
        font_path = resolve_font_path('NotoSansHans-Regular.otf')
        logger.info(f"[Font] steam_list 渲染传入字体路径: {font_path}")
        # 修改：显式传递 group_id
        async for result in handle_steam_list(self, event, group_id=group_id, font_path=font_path, proxy=self.proxy):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam config")
    async def steam_config(self, event: AstrMessageEvent):
        '''显示当前插件配置（敏感信息已隐藏）'''
        lines = []
        hidden_keys = {"steam_api_key", "sgdb_api_key"}
        for k, v in self.config.items():
            if k in hidden_keys:
                lines.append(f"{k}: ****** (已隐藏)")
            else:
                lines.append(f"{k}: {v}")
        # 新增：显示智能轮询间隔说明
        if hasattr(self, "smart_poll_intervals"):
            intervals = self.smart_poll_intervals
            lines.append(f"智能轮询间隔（分钟）: {intervals}（依次为[游戏中, 12分钟内, 12分钟~3小时, 3小时~24小时, 24~48小时, 超过48小时]）")
        yield event.plain_result("当前配置：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam set")
    async def steam_set(self, event: AstrMessageEvent, key: str, value: str):
        '''设置配置参数，立即生效（如 steam set fixed_poll_interval 600）'''
        ok, msg = apply_hot_update(self, key, value)
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam rs")
    async def steam_rs(self, event: AstrMessageEvent):
        '''清除所有状态并初始化（重启插件用）'''
        self.group_last_states.clear()
        self.group_last_quit_times.clear()
        self.group_pending_logs.clear()
        self.playing_sessions.clear()
        getattr(self, "_session_meta", {}).clear()
        self.group_recent_games.clear()
        self._superpower_cache.clear()
        self._game_name_cache.clear()
        self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        self.running_groups.clear()
        self.group_monitor_enabled.clear()
        self.group_achievement_enabled.clear()
        self.notify_sessions = {}
        self._save_group_switches()
        self._save_persistent_data(force=True)  # 清空后保存
        yield event.plain_result("Steam状态监控插件已重置，所有状态已清空。")

    async def _render_daily_rank_file(self, rank_data):
        """补齐排行榜展示信息并渲染为临时图片。"""
        sid_set = {player["sid"] for player in rank_data}
        sid_info = {}
        if sid_set:
            status_map = await self.fetch_player_statuses_batch(list(sid_set))
            for sid, info in status_map.items():
                sid_info[sid] = {
                    "name": info.get("name") or sid,
                    "avatar_url": info.get("avatarfull") or info.get("avatar"),
                }

        yesterday = self._get_day_key(-1)
        day_data = self.play_records.get(yesterday, {})
        for player in rank_data:
            sid = player["sid"]
            info = sid_info.get(sid, {})
            player["name"] = self._resolve_bind_name(
                sid,
                info.get("name", sid[-8:]),
            )
            player["avatar_url"] = info.get("avatar_url")
            player["top_game_id"] = None
            if not player["games"]:
                continue
            top_name = player["games"][0]["name"]
            for game_id, game_info in day_data.get(sid, {}).items():
                if game_info.get("name") == top_name:
                    player["top_game_id"] = game_id
                    break

        async def cover_fetcher(gameid):
            return await self.get_game_cover_url(gameid)

        avatar_frame_paths = {}
        from ..presentation.renderers.game_start import get_avatar_frame_path, get_avatar_frame_url

        for player in rank_data:
            sid = player.get("sid", "")
            if not sid:
                continue
            frame_path = await get_avatar_frame_path(
                self.data_dir,
                sid,
                proxy=self.proxy,
            )
            if not frame_path:
                frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                if frame_url:
                    frame_path = await get_avatar_frame_path(
                        self.data_dir,
                        sid,
                        frame_url,
                        proxy=self.proxy,
                    )
            if frame_path:
                avatar_frame_paths[sid] = frame_path

        # 排行榜游戏名统一转中文名来源（覆盖插件重启/缓存污染等写入的英文名）
        for p in rank_data:
            for g in p.get("games", []):
                gid = g.get("gameid")
                if not gid:
                    continue
                resolved = await self.get_chinese_game_name(str(gid), g.get("name"))
                if resolved:
                    g["name"] = resolved

        font_path = resolve_font_path("NotoSansHans-Regular.otf")
        img_bytes = await render_rank_image(
            self.data_dir,
            rank_data,
            "昨日",
            font_path=font_path,
            proxy=self.proxy,
            cover_fetcher=cover_fetcher,
            avatar_frame_paths=avatar_frame_paths,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(img_bytes)
            return tmp.name

    async def _daily_rank_push(self, test_mode=False):
        """推送昨日榜单；默认按目标群独立聚合，显式全局模式才共享总榜。"""
        use_global_rank = getattr(self, "rank_push_all", False)
        scopes = build_rank_push_scopes(
            getattr(self, "rank_push_groups", []),
            use_global_rank=use_global_rank,
        )
        if not scopes:
            logger.warning(
                "[排行榜] 没有目标群可推送"
                "（请先使用 /steam rank_on 或 /steam rank_on all 开启推送）"
            )
            return

        rendered_files = {}
        try:
            for target_group_id, data_group_id in scopes:
                render_key = (
                    ("global", None)
                    if data_group_id is None
                    else ("group", data_group_id)
                )
                if render_key not in rendered_files:
                    rank_data = self._get_rank_data(
                        days=1,
                        group_id=data_group_id,
                        base_day_offset=-1,
                    )
                    if not rank_data:
                        scope_label = (
                            "全部群"
                            if data_group_id is None
                            else f"群 {data_group_id}"
                        )
                        logger.info(
                            f"[排行榜] {scope_label}昨日无游玩记录，跳过推送"
                        )
                        rendered_files[render_key] = None
                    else:
                        try:
                            rendered_files[render_key] = (
                                await self._render_daily_rank_file(rank_data)
                            )
                        except Exception as e:
                            logger.error(
                                f"[排行榜] 渲染群 {data_group_id or '全局'} "
                                f"昨日榜单失败: {e}"
                            )
                            rendered_files[render_key] = None

                tmp_path = rendered_files[render_key]
                if not tmp_path:
                    continue
                try:
                    session = getattr(self, "notify_sessions", {}).get(
                        target_group_id
                    )
                    if not is_sendable_group_session(session):
                        logger.warning(
                            f"[排行榜] 群 {target_group_id} 未找到有效推送会话，跳过"
                        )
                        continue
                    await self.context.send_message(
                        session,
                        MessageChain([
                            Plain("📊 昨日游戏时长排行榜来啦！\n"),
                            Image.fromFileSystem(tmp_path),
                        ]),
                    )
                    logger.info(
                        f"[排行榜] 已推送昨日排行榜到群 {target_group_id}"
                    )
                except Exception as e:
                    logger.error(
                        f"[排行榜] 推送群 {target_group_id} 失败: {e}"
                    )
        except Exception as e:
            logger.error(f"[排行榜] 每日推送异常: {e}")
        finally:
            for tmp_path in {
                path for path in rendered_files.values() if path
            }:
                try:
                    os.unlink(tmp_path)
                except OSError as e:
                    logger.warning(
                        f"[排行榜] 清理临时图片失败 {tmp_path}: {e}"
                    )
    async def _render_and_send_rank(self, event, group_id, days, period_label, is_all=False):
        """生成排行榜图片并发送"""
        try:
            rank_data = self._get_rank_data(days=days, group_id=None if is_all else group_id)
            if not rank_data:
                yield event.plain_result(f"暂无{period_label}游玩记录，玩家游戏结束后才会有数据。")
                return
            # 补充玩家昵称和头像URL
            sid_set = {p["sid"] for p in rank_data}
            sid_info = {}
            if sid_set:
                status_map = await self.fetch_player_statuses_batch(list(sid_set))
                for sid, info in status_map.items():
                    sid_info[sid] = {
                        "name": info.get("name") or sid,
                        "avatar_url": info.get("avatarfull") or info.get("avatar")
                    }
            for p in rank_data:
                info = sid_info.get(p["sid"], {})
                p["name"] = self._resolve_bind_name(p["sid"], info.get("name", p["sid"][-8:]))
                p["avatar_url"] = info.get("avatar_url")
                # 标记主玩游戏ID用于封面获取
                if p["games"]:
                    # 需要gameid来获取封面，从play_records中反查
                    p["top_game_id"] = None
            # 从play_records中反查每个玩家top游戏的gameid
            for p in rank_data:
                if not p["games"]:
                    continue
                top_name = p["games"][0]["name"]
                # 在最近数据中找匹配的gameid
                for di in range(days):
                    dk = self._get_day_key(-di)
                    day_data = self.play_records.get(dk, {})
                    sid_games = day_data.get(p["sid"], {})
                    for gid, ginfo in sid_games.items():
                        if ginfo.get("name") == top_name:
                            p["top_game_id"] = gid
                            break
                    if p.get("top_game_id"):
                        break

            # 封面获取回调
            async def cover_fetcher(gameid):
                return await self.get_game_cover_url(gameid)

            # 获取头像框路径
            avatar_frame_paths = {}
            from ..presentation.renderers.game_start import get_avatar_frame_url, get_avatar_frame_path
            for p in rank_data:
                sid = p.get("sid", "")
                if sid:
                    fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
                    if not fp:
                        frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                        if frame_url:
                            fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
                    if fp:
                        avatar_frame_paths[sid] = fp

            # 排行榜游戏名统一转中文名来源（覆盖插件重启/缓存污染等写入的英文名）
            for p in rank_data:
                for g in p.get("games", []):
                    gid = g.get("gameid")
                    if not gid:
                        continue
                    resolved = await self.get_chinese_game_name(str(gid), g.get("name"))
                    if resolved:
                        g["name"] = resolved

            font_path = resolve_font_path('NotoSansHans-Regular.otf')
            img_bytes = await render_rank_image(
                self.data_dir, rank_data, period_label,
                font_path=font_path, proxy=self.proxy,
                cover_fetcher=cover_fetcher,
                avatar_frame_paths=avatar_frame_paths
            )
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        except Exception as e:
            logger.error(f"[排行榜] 渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"排行榜生成失败: {e}")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam rank")
    async def steam_rank(self, event: AstrMessageEvent, period: str = ""):
        '''查看本群玩家游戏时长排行榜（默认今日，可选 week/month）'''
        group_id = event.get_group_id() or "default"
        period = period.strip().lower()
        if period == "week":
            days, label = 7, "最近7天"
        elif period == "month":
            days, label = 30, "最近30天"
        elif period.isdigit():
            days = int(period)
            if days <= 0:
                days = 1
            label = f"最近{days}天"
        else:
            days, label = 1, "今日"
        async for result in self._render_and_send_rank(event, group_id, days, label, is_all=False):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam allrank")
    async def steam_allrank(self, event: AstrMessageEvent, period: str = ""):
        '''查看所有群玩家游戏时长排行榜（默认今日，可选 week/month）'''
        period = period.strip().lower()
        if period == "week":
            days, label = 7, "最近7天"
        elif period == "month":
            days, label = 30, "最近30天"
        elif period.isdigit():
            days = int(period)
            if days <= 0:
                days = 1
            label = f"最近{days}天"
        else:
            days, label = 1, "今日"
        async for result in self._render_and_send_rank(event, None, days, label, is_all=True):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam rank_on")
    async def steam_rank_on(self, event: AstrMessageEvent, param: str = ""):
        '''每日排行榜推送管理；参数: all=全局排行, list=查看状态, test=即刻推送, del [群号]=删除推送'''
        param = param.strip().lower()
        if param == "list":
            is_all = getattr(self, 'rank_push_all', False)
            groups = list(self.rank_push_groups)
            if groups:
                mode = '全局' if is_all else '分群'
                yield event.plain_result(f"当前排行榜推送模式：{mode}排行，推送群：{', '.join(groups)}")
            else:
                yield event.plain_result("当前未开启任何排行榜推送。使用 /steam rank_on 或 /steam rank_on all 开启。")
            return
        if param == "test":
            yield event.plain_result("正在生成昨日排行榜，稍等...")
            await self._daily_rank_push(test_mode=True)
            return
        if param.startswith("del"):
            parts = param.split()
            if len(parts) >= 2:
                target = parts[1]
            else:
                target = event.get_group_id() or "default"
            if target in self.rank_push_groups:
                self.rank_push_groups.remove(target)
                self._save_rank_push_groups()
                yield event.plain_result(f"已关闭群 {target} 的每日排行榜推送。")
            else:
                yield event.plain_result(f"群 {target} 未在推送列表中。")
            return
        if param == "all":
            self.rank_push_all = True
            group_id = event.get_group_id() or "default"
            if not is_valid_group_id(group_id):
                yield event.plain_result("请在群聊中开启排行榜推送。")
                return
            if group_id not in self.rank_push_groups:
                self.rank_push_groups.append(group_id)
            self._save_rank_push_groups()
            yield event.plain_result("已开启每日排行榜自动推送（全局排行）")
        else:
            self.rank_push_all = False
            group_id = event.get_group_id() or "default"
            if not is_valid_group_id(group_id):
                yield event.plain_result("请在群聊中开启排行榜推送。")
                return
            if group_id not in self.rank_push_groups:
                self.rank_push_groups.append(group_id)
                self._save_rank_push_groups()
            yield event.plain_result(f"已开启本群每日排行榜自动推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单同步")
    async def steam_qq_menu_sync(self, event: AstrMessageEvent):
        """创建或更新 QQ 官方机器人指令面板。"""
        yield event.plain_result(await self.qq_menu_sync(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单状态")
    async def steam_qq_menu_status(self, event: AstrMessageEvent):
        """查询 QQ 官方机器人指令面板状态。"""
        yield event.plain_result(await self.qq_menu_status(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单删除")
    async def steam_qq_menu_delete(self, event: AstrMessageEvent):
        """删除本插件记录的 QQ 官方机器人指令面板。"""
        yield event.plain_result(await self.qq_menu_delete(event))

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam help")
    async def steam_help(self, event: AstrMessageEvent):
        '''显示所有指令帮助'''
        help_text = (
            "Steam状态监控插件指令：\n"
            "/steam on - 启动监控\n"
            "/steam off - 停止监控\n"
            "/steam price [游戏名或Steam链接] - 查询游戏价格、史低与地区对比\n"
            "/steam px [游戏名] - 价格查询快捷版，直接返回第一条匹配\n"
            "/steam list - 列出所有玩家状态\n"
            "/steam config - 查看当前配置\n"
            "/steam set [参数] [值] - 设置配置参数\n"
            "/steam addid [SteamID] - 添加SteamID\n"
            "/steam delid [SteamID] - 删除SteamID\n"
            "/steam push_group [SteamID] - 添加id到联动推送的副群\n"
            "/steam delpush_group [SteamID] [群号可选] - 删除id联动推送的副群，可指定群号\n"
            "/steam openbox [SteamID] - 查看指定SteamID的全部信息\n"
            "/steam rank - 查看本群今日游戏时长排行榜\n"
            "/steam rank 天数 - 查看本群指定天数排行榜（如 7, 30）\n"
            "/steam allrank - 查看所有群今日排行榜\n"
            "/steam allrank 天数 - 查看所有群指定天数排行榜\n"
            "/steam alllist [img|text] - 查看所有群聊玩家状态（默认图片，text 纯文本）\n"
            "/steam rank_on [all|list|test|del] - 管理每日排行榜推送（可配置时间）\n"
            "/steam rank_on list - 查看推送状态\n"
            "/steam rank_on del [群号] - 删除指定群推送（默认本群）\n"
            "/steam fonts - 查看字体包状态（检测CJK字体是否就绪）\n"
            "/steam fonts download - 立即下载字体包\n"
            "/steam fonts clean - 清理已下载字体缓存\n"
            "/steam rs - 清除状态并初始化\n"
            "/steamwho @用户 / 在干嘛 @用户 - 即时查询绑定玩家的Steam状态\n"
            "/steam help - 显示本帮助\n"
        )
        yield event.plain_result(help_text)

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam openbox")
    async def steam_openbox(self, event: AstrMessageEvent, steamid: str):
        '''查询指定SteamID的全部API返回信息'''
        if not self.API_KEY:
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        sid = await self.resolve_steam_input(steamid)
        if not sid or not sid.isdigit() or len(sid) != 17:
            yield event.plain_result("无法解析为有效SteamID，支持格式：17位SteamID64 / 个人资料链接 / 自定义ID链接 / 8位好友码")
            return
        async for result in handle_openbox(self, event, sid):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steamwho")
    async def steam_who(self, event: AstrMessageEvent, qq: str):
        '''查询指定QQ绑定的Steam玩家状态（ /steamwho @用户 或 /在干嘛 @用户 ）'''

        m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', qq.strip()); qq_clean = m.group(1) or m.group(2) or m.group(3) or m.group(4) if m else qq.strip().lstrip('@')
        info = getattr(self, "_bind_data", {}).get(qq_clean)
        if not info:
            yield event.plain_result(f"QQ {qq_clean} 未绑定任何SteamID，请先使用 /steam addid SteamID @{qq_clean}")
            return
        sid = info.get("sid", "")
        if not sid:
            yield event.plain_result(f"QQ {qq_clean} 的绑定数据异常")
            return
        status = await self.fetch_player_status(sid)
        if not status:
            yield event.plain_result(f"无法获取 {sid} 的Steam状态")
            return
        name = self._resolve_bind_name(sid, status.get('name') or sid)
        gameid = status.get('gameid')
        game = status.get('gameextrainfo')
        zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or '')
        now = int(time.time())
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        start_time = self.session_service.started_at(group_id, sid, gameid) if gameid else None
        user_list = [build_player_row(
            sid,
            status,
            name=name,
            zh_game_name=zh_game_name,
            start_time=start_time,
            now=now,
        )]
        # 获取头像框
        from ..presentation.renderers.game_start import get_avatar_frame_url, get_avatar_frame_path
        avatar_frame_paths = {}
        fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
        if not fp:
            frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
            if frame_url: fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
        if fp: avatar_frame_paths[sid] = fp
        # 渲染列表卡片（新版steam风格不展示封面；旧版卡片风格需要封面，仅在关闭新风格时预取）
        from ..presentation.renderers.steam_list import render_steam_list_image
        font_path = resolve_font_path('NotoSansHans-Regular.otf')
        steam_style = self.config.get('enable_steam_style', False)
        covers = {}
        if not steam_style and gameid:
            from ..presentation.renderers.game_start import get_cover_path
            cp = await get_cover_path(
                self.data_dir, gameid, game or zh_game_name,
                sgdb_api_key=self.SGDB_API_KEY,
                appid=gameid,
                proxy=self.proxy,
                sgdb_api_base=self.SGDB_API_BASE,
            )
            if cp: covers[sid] = cp
        parent_name, parent_avatar_url = self._steam_parent(event)
        img_bytes = await render_steam_list_image(self.data_dir, user_list, font_path=font_path, proxy=self.proxy, avatar_frame_paths=avatar_frame_paths, covers=covers, steam_style=steam_style, parent_name=parent_name, parent_avatar_url=parent_avatar_url)
        if img_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        else:
            yield event.plain_result("渲染图片失败")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("在干嘛")
    async def steam_zai_gan_ma(self, event: AstrMessageEvent, qq: str):
        '''/在干嘛 @用户 —— steamwho 的别名'''
        async for r in self.steam_who(event, qq):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam off")
    async def steam_off(self, event: AstrMessageEvent):
        '''彻底停止本群Steam状态监控轮询，释放轮询资源'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        self.group_monitor_enabled[group_id] = False
        if group_id in self.running_groups:
            self.running_groups.remove(group_id)
        self._save_group_switches()
        # 清除该群的轮询时间表，停止轮询（/steam on 后会重新初始化）
        self.next_poll_time.pop(group_id, None)
        # 停用后不再推送本群缓冲通知；会话仍保留，由 tick_due 到期结算时长
        self._pending_end_notifications.pop(group_id, None)
        # 取消该群所有成就轮询任务，释放资源
        keys_to_cancel = [k for k in list(self.achievement_poll_tasks.keys()) if k[0] == group_id]
        for key in keys_to_cancel:
            task = self.achievement_poll_tasks.pop(key, None)
            if task:
                task.cancel()
        yield event.plain_result(f"已为本群彻底关闭Steam监控，轮询已停止。使用 /steam on 可重新启动。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam achievement_on")
    async def steam_achievement_on(self, event: AstrMessageEvent):
        """开启本群Steam成就推送"""
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        self.group_achievement_enabled[group_id] = True
        self._save_group_switches()
        yield event.plain_result(f"已为本群开启Steam成就推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam achievement_off")
    async def steam_achievement_off(self, event: AstrMessageEvent):
        """关闭本群Steam成就推送"""
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        self.group_achievement_enabled[group_id] = False
        self._save_group_switches()
        yield event.plain_result(f"已为本群关闭Steam成就推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_achievement_render")
    async def steam_test_achievement_render(self, event: AstrMessageEvent, steamid: str, gameid: int, count: int = 3):
        '''测试成就消息渲染效果（steam test_achievement_render [steamid] [gameid] [数量]）'''
        player_name = steamid
        game_name = await self.get_chinese_game_name(gameid)
        group_id = self.GROUP_ID or 'default'
        achievements = await self.achievement_monitor.get_player_achievements(self.API_KEY, group_id, steamid, gameid)
        if not achievements:
            yield event.plain_result("未获取到任何成就，可能为隐私或无成就。")
            return
        details = await self.achievement_monitor.get_achievement_details(group_id, gameid, lang="schinese", api_key=self.API_KEY, steamid=steamid)
        import random
        count = max(1, min(count, len(achievements)))
        unlocked = set(random.sample(list(achievements), count))
        font_path = resolve_font_path('NotoSansHans-Regular.otf')
        # 直接测试 Pillow 渲染
        try:
            img_bytes = await self.achievement_monitor.render_achievement_image(details, unlocked, player_name=player_name, font_path=font_path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        except Exception as e:
            import traceback
            logger.error(f"成就图片渲染失败: {e}\n{traceback.format_exc()}")
            # 回退文本
            msg = self.achievement_monitor.render_achievement_message(details, unlocked, player_name=player_name)
            yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_game_start_render")
    async def test_game_start_render(self, event: AstrMessageEvent, steamid: str, gameid: int):
        '''测试开始游戏图片渲染效果（steam test_game_start_render [steamid] [gameid]）'''
        try:
            status = await self.fetch_player_status(steamid)
            player_name = self._resolve_bind_name(steamid, status.get("name") if status else steamid)
            avatar_url = status.get("avatarfull") or status.get("avatar") or "" if status else ""
            zh_game_name, en_game_name = await self.get_game_names(gameid)
            logger.info(f"[测试开始游戏渲染] steamid={steamid} gameid={gameid} player_name={player_name} avatar_url={avatar_url} zh_game_name={zh_game_name} en_game_name={en_game_name}")
            superpower = self.get_today_superpower(steamid)
            print(f"[superpower] test_game_start_render superpower={superpower}")
            font_path = resolve_font_path('NotoSansHans-Regular.otf')
            online_count = await self.get_game_online_count(gameid)
            img_bytes = await render_game_start(
                self.data_dir, steamid, player_name, avatar_url, gameid, zh_game_name, api_key=self.API_KEY, superpower=superpower, sgdb_api_key=self.SGDB_API_KEY, font_path=font_path, sgdb_game_name=en_game_name, online_count=online_count, appid=gameid
                , proxy=self.proxy, version=self._plugin_version, sgdb_api_base=self.SGDB_API_BASE, steam_store_base=self.STEAM_STORE_BASE)
            logger.info(f"[测试开始游戏渲染] render_game_start 返回类型: {type(img_bytes)} 长度: {len(img_bytes) if img_bytes else 'None'}")
            if img_bytes:
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                img = PILImage.open(tmp_path).convert("RGB")
                cropped_img = self.crop_image_auto(img, bg_color=(51,81,66), threshold=15)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp2:
                    cropped_img.save(tmp2, format="PNG")
                    tmp_path = tmp2.name
                logger.info(f"[测试开始游戏渲染] 已保存裁剪图到 {tmp_path}")
                yield event.image_result(tmp_path)
            else:
                yield event.plain_result("渲染失败，未获取到图片数据。")
        except Exception as e:
            logger.error(f"测试开始游戏图片渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"渲染异常: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_game_end_render")
    async def steam_test_game_end_render(self, event: AstrMessageEvent, steamid: str, gameid: int, duration_min: float = 120, end_time: str = None, tip_text: str = None):
        '''测试游戏结束图片渲染（steam test_game_end_render [steamid] [gameid] [时长分钟] [结束时间 可选] [提示 可选]）'''
        try:
            status = await self.fetch_player_status(steamid)
            player_name = self._resolve_bind_name(steamid, status.get("name") if status else steamid)
            avatar_url = status.get("avatarfull") or status.get("avatar") or "" if status else ""
            zh_game_name, en_game_name = await self.get_game_names(gameid)
            logger.info(f"[get_game_names] zh_game_name={zh_game_name}, en_game_name={en_game_name}")  # 新增英文名输出
            from datetime import datetime
            if end_time:
                end_time_str = end_time
            else:
                end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            duration_h = float(duration_min) / 60 if duration_min else 0
            if not tip_text:
                if duration_min < 5:
                    tip_text = "风扇都没转热，主人就结束了？"
                elif duration_min < 10:
                    tip_text = "杂鱼杂鱼~主人你就这水平？"
                elif duration_min < 30:
                    tip_text = "热身一下就结束了？"
                elif duration_min < 60:
                    tip_text = "歇会儿再来，别太累了喵！"
                elif duration_min < 120:
                    tip_text = "沉浸在游戏世界，时间过得飞快喵！"
                elif duration_min < 300:
                    tip_text = "肝到手软了喵！主人不如陪陪咱~"
                elif duration_min < 600:
                    tip_text = "你吃饭了吗？还是说你已经忘了吃饭这件事？"
                elif duration_min < 1200:
                    tip_text = "家里电费都要被你玩光了喵！"
                elif duration_min < 1800:
                    tip_text = "咱都要给你颁发‘不眠猫’勋章了！"
                elif duration_min < 2400:
                    tip_text = "主人你还活着喵？你是不是忘了关电脑呀~"
                else:
                    tip_text = "你已经和椅子合为一体，成为传说中的‘椅子精’了喵！"
            font_path = resolve_font_path('NotoSansHans-Regular.otf')
            img_bytes = await render_game_end(
                self.data_dir, steamid, player_name, avatar_url, gameid, zh_game_name,
                end_time_str, tip_text, duration_h, sgdb_api_key=self.SGDB_API_KEY, font_path=font_path, sgdb_game_name=en_game_name, appid=gameid
            , proxy=self.proxy, api_key=self.API_KEY)
            msg = f"👋 {player_name} 不玩 {zh_game_name} 了\n游玩时间 {duration_h:.1f}小时"
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.plain_result(msg)
            yield event.image_result(tmp_path)
        except Exception as e:
            import traceback
            logger.error(f"测试游戏结束图片渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"渲染异常: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam fonts")
    async def steam_fonts(self, event: AstrMessageEvent, param: str = ""):
        '''字体包管理；参数: download=立即下载, clean=清理缓存'''
        service = getattr(self, "font_pack", None)
        if service is None:
            yield event.plain_result("字体服务未初始化。")
            return
        action = (param or "").strip().lower()
        if action in ("", "status"):
            yield event.plain_result(service.format_status_text())
            return
        if action == "clean":
            yield event.plain_result(service.clean())
            return
        if action in ("download", "update"):
            last_sent = 0.0

            async def progress_cb(payload):
                nonlocal last_sent
                now = time.time()
                if not payload.get("force") and now - last_sent < 2:
                    return
                last_sent = now
                text = (
                    "正在下载字体包...\n"
                    f"{payload['bar']}\n"
                    f"预计剩余：{payload['eta_text']}"
                )
                try:
                    await event.send(event.plain_result(text))
                except Exception:
                    logger.info("[Font] %s", text.replace("\n", " "))

            yield event.plain_result("开始下载字体包，完成后会更新状态。")
            result = await service.download_now(progress_cb=progress_cb)
            yield event.plain_result(result)
            return
        yield event.plain_result("用法：/steam fonts、/steam fonts download、/steam fonts clean")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam清除缓存")
    async def steam_clear_cache(self, event: AstrMessageEvent):
        '''清除所有头像、封面图等图片缓存（慎用）'''
        try:
            cache_dirs = [
                os.path.join(self.data_dir, "avatars"),
                os.path.join(self.data_dir, "covers"),
                os.path.join(self.data_dir, "covers_v"),
            ]
            cleared = []
            for d in cache_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d)
                    cleared.append(d)
            msg = "已清除以下缓存目录：\n" + "\n".join(cleared) if cleared else "未找到任何缓存目录，无需清理。"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"清除缓存失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam clear_allids")
    async def steam_clear_allids(self, event: AstrMessageEvent):
        '''删除所有群聊的所有已监控SteamID，并清空相关状态数据'''
        for task in self.achievement_poll_tasks.values():
            task.cancel()
        self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        self.achievement_fail_count.clear()
        self.group_steam_ids.clear()
        self.push_groups.clear()
        self.running_groups.clear()
        self.group_monitor_enabled.clear()
        self.group_achievement_enabled.clear()
        self._save_group_switches()
        self.next_poll_time.clear()
        self.group_last_states.clear()
        self.group_last_quit_times.clear()
        self.group_pending_logs.clear()
        self.playing_sessions.clear()
        getattr(self, "_session_meta", {}).clear()
        self.group_recent_games.clear()
        self._pending_end_notifications.clear()
        self.notify_sessions.clear()
        self._save_group_steam_ids()
        self._save_push_groups()
        self._save_notify_session()
        self._save_persistent_data(force=True)
        self.config['group_steam_ids'] = self.group_steam_ids
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result("已删除所有群聊的所有SteamID，相关状态数据已清空。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam clear_groupids")
    async def steam_clear_groupids(self, event: AstrMessageEvent, group_id: str):
        '''删除指定群聊的所有已监控SteamID，并清空相关状态数据'''
        has_primary = group_id in self.group_steam_ids
        routed_sids = [
            sid for sid, targets in self.push_groups.items()
            if group_id in {str(target) for target in targets}
        ]
        if not has_primary and not routed_sids:
            yield event.plain_result(f"群聊 {group_id} 未绑定任何SteamID，无需清理。")
            return

        for sid in list(self.group_steam_ids.get(group_id, [])):
            self.push_groups.pop(sid, None)
        for sid in routed_sids:
            targets = [target for target in self.push_groups.get(sid, []) if str(target) != group_id]
            if targets:
                self.push_groups[sid] = targets
            else:
                self.push_groups.pop(sid, None)
        self.group_steam_ids.pop(group_id, None)
        self.group_last_states.pop(group_id, None)
        self.group_last_quit_times.pop(group_id, None)
        self.group_pending_logs.pop(group_id, None)
        self.session_service.discard_group(group_id)
        self.group_recent_games.pop(group_id, None)
        self.next_poll_time.pop(group_id, None)
        self.running_groups.discard(group_id)
        self.group_monitor_enabled.pop(group_id, None)
        self.group_achievement_enabled.pop(group_id, None)
        self.notify_sessions.pop(group_id, None)
        self._save_group_steam_ids()
        self._save_push_groups()
        self._save_notify_session()
        self._save_persistent_data(force=True)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"已删除群聊 {group_id} 的所有SteamID和分发路由，相关状态数据已清空。")

    def _should_skip_game(self, gameid):
        """根据黑白名单配置判断是否应跳过该游戏的监控/播报"""
        if not gameid:
            return False
        mode = self.config.get('game_filter_mode', '全部游戏')
        if mode == '全部游戏':
            return False
        ids_str = self.config.get('game_filter_ids', '')
        if not ids_str or not ids_str.strip():
            return False
        try:
            filter_ids = [x.strip() for x in ids_str.split(',') if x.strip()]
        except Exception:
            return False
        if mode == '白名单':
            return str(gameid) not in filter_ids
        elif mode == '黑名单':
            return str(gameid) in filter_ids
        return False

    def _get_day_key(self, offset_days=0):
        """基于凌晨4:00边界的日期键。"""
        return self.ranking_service.day_key(offset_days)

    def _get_rank_data(self, days=1, group_id=None, base_day_offset=0):
        """聚合游玩时长数据，返回已排序的排行榜列表。"""
        ranking = self.ranking_service
        return ranking.aggregate(
            days=days,
            sids=ranking.target_sids(group_id),
            base_day_offset=base_day_offset,
        )

    def _record_playtime(self, sid, gameid, game_name, duration_min):
        """记录游玩时长到 play_records，带5分钟去重（防止多群重复记录）"""
        self.ranking_service.record_playtime(sid, gameid, game_name, duration_min)

    async def get_game_online_count(self, gameid):
        '''通过 Steam Web API 获取当前游戏在线人数'''
        if not gameid:
            return None
        url = f"{self.STEAM_API_BASE}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={gameid}"
        try:
            async with httpx.AsyncClient(timeout=10, **httpx_client_kwargs(self.proxy)) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get('response', {}).get('player_count')
        except Exception as e:
            logger.warning(f"获取在线人数失败: {e} (gameid={gameid})")
        return None

    def _steam_parent(self, event):
        """返回 (触发者昵称, QQ头像URL)；获取失败返回 (None, None)。用于 Steam 列表顶部显示触发者头像/名称。"""
        try:
            _sid = event.get_sender_id()
            _name = event.get_sender_name()
        except Exception:
            return None, None
        url = f"https://q1.qlogo.cn/g?b=qq&nk={_sid}&s=640" if _sid else None
        return _name, url

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam alllist")
    async def steam_alllist(self, event: AstrMessageEvent, mode: str = "img"):
        '''所有群聊玩家状态（默认图片，steam alllist text 输出文本）'''
        from ..presentation.renderers.steam_list import render_steam_list_image
        from ..presentation.renderers.game_start import get_avatar_frame_url, get_avatar_frame_path
        user_list = []
        now = int(time.time())
        all_sids = []
        for gid_ in self.group_steam_ids:
            all_sids.extend(self.group_steam_ids[gid_])
        status_map = await self.fetch_player_statuses_batch(all_sids) if all_sids else {}
        for group_id, steam_ids in self.group_steam_ids.items():
            next_poll = self.next_poll_time.get(group_id, {})
            for sid in steam_ids:
                nt = next_poll.get(sid, now)
                sl = int(nt - now)
                p_str = f"下次轮询{sl}秒后" if sl < 60 else f"下次轮询{sl//60}分钟后"
                status = status_map.get(sid)
                name = self._resolve_bind_name(sid, (status or {}).get("name") or sid)
                gameid = (status or {}).get("gameid")
                game = (status or {}).get("gameextrainfo")
                zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or "未知游戏")
                start_time = self.session_service.started_at(group_id, sid, gameid) if gameid else None
                user_list.append(build_player_row(
                    sid,
                    status,
                    name=name,
                    zh_game_name=zh_game_name,
                    start_time=start_time,
                    now=now,
                    group_id=group_id,
                    poll_str=p_str,
                ))
        # 纯文本输出模式
        if mode.lower() == 'text':
            from ..presentation.renderers.steam_list import get_status_text
            lines = ["=== Steam 全群玩家状态 ===\n"]
            by_group = {}
            for u in user_list:
                by_group.setdefault(u.get('group_id', '?'), []).append(u)
            for gid, members in by_group.items():
                lines.append(f"📋 群: {gid}")
                for u in members:
                    sid_shown = u['sid']
                    sicon = {'playing': '🎮', 'online': '🔵', 'offline': '💤',
                             'busy': '🔴', 'away': '🟣', 'snooze': '🟣', 'error': '⚠️'}.get(u['status'], '❓')
                    name = u['name']
                    stext = get_status_text(u['status'])
                    detail = f" 正在玩：{u['game']}" if u['status'] == 'playing' and u.get('game') else ""
                    play = f" | 时长：{u['play_str']}" if u.get('play_str') else ""
                    offline_info = f" | {u['play_str']}" if u['status'] == 'offline' and u.get('play_str') else ""
                    poll = f" | {u.get('poll_str','')}" if u.get('poll_str') else ""
                    lines.append(f"  {sicon} {name} {stext}{detail}{play}{offline_info}")
                    lines.append(f"     ID: {sid_shown}{poll}")
                lines.append("")
            online_count = sum(1 for u in user_list if u['status'] in ('playing','online','away','snooze','busy'))
            lines.append(f"📊 在线: {online_count} / 总数: {len(user_list)}")
            yield event.plain_result("\n".join(lines))
            return
        # 图片输出模式（默认）
        # 按状态排序：游戏中 > 在线 > 忙碌 > 离开/打盹 > 离线 > 异常
        _status_rank = {'playing': 0, 'online': 1, 'busy': 2, 'away': 3, 'snooze': 4, 'offline': 5, 'error': 6}
        user_list.sort(key=lambda u: _status_rank.get(u['status'], 9))
        avatar_frame_paths = {}
        for u in user_list:
            sid = u.get('sid', '')
            if sid:
                fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
                if not fp:
                    frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                    if frame_url:
                        fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
                if fp:
                    avatar_frame_paths[sid] = fp
        font_path = resolve_font_path('NotoSansHans-Regular.otf')
        # 新版steam风格不展示封面；旧版卡片风格需要封面，仅在关闭新风格时预取
        steam_style = self.config.get('enable_steam_style', False)
        covers = {}
        if not steam_style:
            for u in user_list:
                gid = u.get('gameid', '')
                if gid:
                    from ..presentation.renderers.game_start import get_cover_path
                    cp = await get_cover_path(
                        self.data_dir, gid, u.get('game', ''),
                        sgdb_api_key=self.SGDB_API_KEY,
                        appid=gid,
                        proxy=self.proxy,
                        sgdb_api_base=self.SGDB_API_BASE,
                    )
                    if cp:
                        covers[u['sid']] = cp
        parent_name, parent_avatar_url = self._steam_parent(event)
        img_bytes = await render_steam_list_image(self.data_dir, user_list, font_path=font_path, proxy=self.proxy, avatar_frame_paths=avatar_frame_paths, covers=covers, steam_style=steam_style, parent_name=parent_name, parent_avatar_url=parent_avatar_url)
        if img_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        else:
            yield event.plain_result("渲染图片失败")

    def get_today_superpower(self, steamid):
        today = date.today().isoformat()
        cache_key = (steamid, today)
        if cache_key in self._superpower_cache:
            return self._superpower_cache[cache_key]
        if self._abilities is None:
            with open(self._abilities_path, encoding="utf-8") as abilities_file:
                self._abilities = [line.strip() for line in abilities_file if line.strip()]
        superpower = random.Random(f"{steamid}-{today}").choice(self._abilities)
        self._superpower_cache[cache_key] = superpower
        return superpower

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam push_group")
    async def steam_push_group(self, event: AstrMessageEvent, steamid: str):
        '''将本群加入指定SteamID的联动推送组（不重复轮询，仅同步推送）'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        if not steamid.isdigit() or len(steamid) != 17:
            yield event.plain_result("SteamID无效（需为64位数字串，17位）")
            return
        # 检查主群是否已轮询该SteamID
        found = False
        for gid, ids in self.group_steam_ids.items():
            if steamid in ids:
                found = True
                break
        if not found:
            yield event.plain_result("未找到已轮询该SteamID的主群，请先在任一群添加并开启监控。")
            return
        # 记录推送群
        self.push_groups.setdefault(steamid, [])
        if group_id not in self.push_groups[steamid]:
            self.push_groups[steamid].append(group_id)
            self._save_push_groups()
            yield event.plain_result(f"本群已加入SteamID {steamid} 的联动推送组。")
        else:
            yield event.plain_result("本群已在该SteamID的推送组中。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam delpush_group")
    async def steam_delpush_group(self, event: AstrMessageEvent, steamid: str, target_group: str = ''):
        '''将当前群/指定群从SteamID的联动推送组移除；可传 target_group 指定群号'''
        if target_group:
            group_id = target_group.strip()
        else:
            group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        if not steamid.isdigit() or len(steamid) != 17:
            yield event.plain_result("SteamID无效（需为64位数字串，17位）")
            return
        if steamid not in self.push_groups or group_id not in self.push_groups[steamid]:
            yield event.plain_result(f"群 {group_id} 未在 SteamID {steamid} 的推送组中。")
            return
        self.push_groups[steamid].remove(group_id)
        if not self.push_groups[steamid]:
            self.push_groups.pop(steamid)
        self._save_push_groups()
        if target_group:
            yield event.plain_result(f"已从 SteamID {steamid} 的联动推送组中移除群 {group_id}。")
        else:
            yield event.plain_result(f"本群已从 SteamID {steamid} 的联动推送组移除。")
