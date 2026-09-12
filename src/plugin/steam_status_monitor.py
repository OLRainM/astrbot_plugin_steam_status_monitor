from astrbot.api.star import Star, Context
from ..shared.logging import logger
from ..shared.network import httpx_client_kwargs, requests_verify
from astrbot.api.event import filter, AstrMessageEvent
import time
import httpx
import asyncio
import os
import random
from ..application.services.monitor_admin import MonitorAdminService
from ..application.services.monitor_control import MonitorControlService
from ..application.services.ranking import RankingService
from ..application.services.price_query import PriceQueryService
from ..application.services.player_status_view import PlayerStatusViewService
from ..application.services.rank_view import RankViewService
from ..application.services.achievement_monitor import AchievementMonitor
from ..application.services.achievement_tracking import AchievementTrackingMixin
from ..application.services.notification_tracking import NotificationTrackingMixin
from ..application.services.session_quit import SessionQuitMixin
from ..application.services.status_change_tracking import StatusChangeTrackingMixin
from ..application.services.polling_tracking import PollingTrackingMixin
from ..domain.monitoring import MonitorStateStore, StateBackedMonitorMixin, should_skip_game
from PIL import Image as PILImage
import io
from datetime import date
import requests
from ..presentation.commands import monitor, ops, rank, store
from ..presentation.web.admin_api import WebAdminAPI
from ..infrastructure.persistence.plugin_data import PersistenceMixin
from ..infrastructure.fonts import FontPackService
from ..infrastructure.clients.steam import SteamClientMixin
from ..application.services.qq_menu_management import QQMenuManagementMixin
from ..shared.paths import ABILITIES_PATH
from .runtime_config import apply_runtime_config


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
        self.monitor_control = MonitorControlService(self)
        self.monitor_admin = MonitorAdminService(self)
        self.player_status_view = PlayerStatusViewService(self)
        self.rank_view = RankViewService(self)
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

    async def _translate_game_query(self, query: str) -> str:
        return await store.translate_game_query(self, query)

    async def _daily_rank_push(self, test_mode=False):
        """推送昨日榜单；默认按目标群独立聚合，显式全局模式才共享总榜。"""
        await self.rank_view.push_daily()

    def _should_skip_game(self, gameid):
        """根据黑白名单配置判断是否应跳过该游戏的监控/播报"""
        return should_skip_game(self.config, gameid)

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
    @filter.command("steam on")
    async def steam_on(self, event: AstrMessageEvent):
        '''手动启动Steam状态监控轮询（分群）'''
        async for result in monitor.on(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam addid")
    async def steam_addid(self, event: AstrMessageEvent, steamid: str, at_user: str = "", nickname: str = ""):
        '''添加SteamID到本群监控列表（分群），支持逗号分隔多个ID，支持SteamID/个人资料链接/自定义ID/好友码
        末尾可加 @用户 [备注名] 绑定QQ与SteamID'''
        async for result in monitor.addid(self, event, steamid, at_user, nickname):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam delid")
    async def steam_delid(self, event: AstrMessageEvent, steamid: str, group_id_param: str = ""):
        '''从监控组删除SteamID；支持好友码/链接；可选传群号跨群删除：/steam delid [SteamID/好友码/链接] [群号]'''
        async for result in monitor.delid(self, event, steamid, group_id_param):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam game")
    async def steam_game(self, event: AstrMessageEvent, appid: str):
        """查询 Steam 游戏详情并生成详情卡片。"""
        async for result in store.game(self, event, appid):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def steam_price_selection(self, event: AstrMessageEvent):
        """接收群聊或私聊中的候选游戏序号。"""
        async for result in store.handle_selection(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam price")
    async def steam_price(self, event: AstrMessageEvent, query: str):
        """价格查询（多个匹配时列出候选并等待回复序号）。"""
        async for result in store.price(self, event, False, "price"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam px")
    async def steam_px(self, event: AstrMessageEvent, query: str):
        """价格查询快捷版（price 缩写）：无需回复序号，直接返回第一条匹配游戏的价格。"""
        async for result in store.price(self, event, True, "px"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam list")
    async def steam_list(self, event: AstrMessageEvent):
        '''列出本群所有玩家当前状态（分群）'''
        async for result in monitor.list_status(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam config")
    async def steam_config(self, event: AstrMessageEvent):
        '''显示当前插件配置（敏感信息已隐藏）'''
        async for result in ops.config(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam set")
    async def steam_set(self, event: AstrMessageEvent, key: str, value: str):
        '''设置配置参数，立即生效（如 steam set fixed_poll_interval 600）'''
        async for result in ops.set_config(self, event, key, value):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam rs")
    async def steam_rs(self, event: AstrMessageEvent):
        '''清除所有状态并初始化（重启插件用）'''
        async for result in ops.rs(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam rank")
    async def steam_rank(self, event: AstrMessageEvent, period: str = ""):
        '''查看本群玩家游戏时长排行榜（默认今日，可选 week/month）'''
        async for result in rank.rank(self, event, period):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam allrank")
    async def steam_allrank(self, event: AstrMessageEvent, period: str = ""):
        '''查看所有群玩家游戏时长排行榜（默认今日，可选 week/month）'''
        async for result in rank.allrank(self, event, period):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam rank_on")
    async def steam_rank_on(self, event: AstrMessageEvent, param: str = ""):
        '''每日排行榜推送管理；参数: all=全局排行, list=查看状态, test=即刻推送, del [群号]=删除推送'''
        async for result in rank.rank_on(self, event, param):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单同步")
    async def steam_qq_menu_sync(self, event: AstrMessageEvent):
        """创建或更新 QQ 官方机器人指令面板。"""
        async for result in ops.qq_menu_sync(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单状态")
    async def steam_qq_menu_status(self, event: AstrMessageEvent):
        """查询 QQ 官方机器人指令面板状态。"""
        async for result in ops.qq_menu_status(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单删除")
    async def steam_qq_menu_delete(self, event: AstrMessageEvent):
        """删除本插件记录的 QQ 官方机器人指令面板。"""
        async for result in ops.qq_menu_delete(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam help")
    async def steam_help(self, event: AstrMessageEvent):
        '''显示所有指令帮助'''
        async for result in ops.help(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam openbox")
    async def steam_openbox(self, event: AstrMessageEvent, steamid: str):
        '''查询指定SteamID的全部API返回信息'''
        async for result in store.openbox(self, event, steamid):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steamwho")
    async def steam_who(self, event: AstrMessageEvent, qq: str):
        '''查询指定QQ绑定的Steam玩家状态（ /steamwho @用户 或 /在干嘛 @用户 ）'''
        async for result in monitor.who(self, event, qq):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("在干嘛")
    async def steam_zai_gan_ma(self, event: AstrMessageEvent, qq: str):
        '''/在干嘛 @用户 —— steamwho 的别名'''
        async for result in monitor.who(self, event, qq):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam off")
    async def steam_off(self, event: AstrMessageEvent):
        '''彻底停止本群Steam状态监控轮询，释放轮询资源'''
        async for result in monitor.off(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam achievement_on")
    async def steam_achievement_on(self, event: AstrMessageEvent):
        """开启本群Steam成就推送"""
        async for result in monitor.achievement_on(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam achievement_off")
    async def steam_achievement_off(self, event: AstrMessageEvent):
        """关闭本群Steam成就推送"""
        async for result in monitor.achievement_off(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_achievement_render")
    async def steam_test_achievement_render(self, event: AstrMessageEvent, steamid: str, gameid: int, count: int = 3):
        '''测试成就消息渲染效果（steam test_achievement_render [steamid] [gameid] [数量]）'''
        async for result in ops.test_achievement_render(self, event, steamid, gameid, count):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_game_start_render")
    async def test_game_start_render(self, event: AstrMessageEvent, steamid: str, gameid: int):
        '''测试开始游戏图片渲染效果（steam test_game_start_render [steamid] [gameid]）'''
        async for result in ops.test_game_start_render(self, event, steamid, gameid):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_game_end_render")
    async def steam_test_game_end_render(self, event: AstrMessageEvent, steamid: str, gameid: int, duration_min: float = 120, end_time: str = None, tip_text: str = None):
        '''测试游戏结束图片渲染（steam test_game_end_render [steamid] [gameid] [时长分钟] [结束时间 可选] [提示 可选]）'''
        async for result in ops.test_game_end_render(self, event, steamid, gameid, duration_min, end_time, tip_text):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam fonts")
    async def steam_fonts(self, event: AstrMessageEvent, param: str = ""):
        '''字体包管理；参数: download=立即下载, clean=清理缓存'''
        async for result in ops.fonts(self, event, param):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam清除缓存")
    async def steam_clear_cache(self, event: AstrMessageEvent):
        '''清除所有头像、封面图等图片缓存（慎用）'''
        async for result in ops.clear_cache(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam clear_allids")
    async def steam_clear_allids(self, event: AstrMessageEvent):
        '''删除所有群聊的所有已监控SteamID，并清空相关状态数据'''
        async for result in monitor.clear_allids(self, event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam clear_groupids")
    async def steam_clear_groupids(self, event: AstrMessageEvent, group_id: str):
        '''删除指定群聊的所有已监控SteamID，并清空相关状态数据'''
        async for result in monitor.clear_groupids(self, event, group_id):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam alllist")
    async def steam_alllist(self, event: AstrMessageEvent, mode: str = "img"):
        '''所有群聊玩家状态（默认图片，steam alllist text 输出文本）'''
        async for result in monitor.alllist(self, event, mode):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam push_group")
    async def steam_push_group(self, event: AstrMessageEvent, steamid: str):
        '''将本群加入指定SteamID的联动推送组（不重复轮询，仅同步推送）'''
        async for result in monitor.push_group(self, event, steamid):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam delpush_group")
    async def steam_delpush_group(self, event: AstrMessageEvent, steamid: str, target_group: str = ''):
        '''将当前群/指定群从SteamID的联动推送组移除；可传 target_group 指定群号'''
        async for result in monitor.delpush_group(self, event, steamid, target_group):
            yield result
