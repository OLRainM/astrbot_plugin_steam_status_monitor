import tempfile
from typing import List, Optional, Tuple

from ...presentation.renderers.game_start import get_avatar_frame_path, get_avatar_frame_url
from ...presentation.renderers.rank import render_rank_image
from ...shared.fonts import resolve_font_path


def parse_rank_period(period: str = "") -> Tuple[int, str]:
    period = (period or "").strip().lower()
    if period == "week":
        return 7, "最近7天"
    if period == "month":
        return 30, "最近30天"
    if period.isdigit():
        days = int(period) or 1
        if days <= 0:
            days = 1
        return days, f"最近{days}天"
    return 1, "今日"


def lookup_top_game_id(play_records, ranking, sid, top_name, days, base_day_offset) -> Optional[str]:
    for index in range(days):
        day_data = play_records.get(ranking.day_key(base_day_offset - index), {})
        for game_id, game_info in day_data.get(sid, {}).items():
            if game_info.get("name") == top_name:
                return game_id
    return None


class RankViewService:
    """排行展示：补昵称/头像/主玩游戏，再出图。不记账。"""

    def __init__(self, plugin):
        self._plugin = plugin

    async def enrich(self, rank_data: List[dict], *, days: int, base_day_offset: int = 0) -> List[dict]:
        plugin = self._plugin
        ranking = plugin.ranking_service
        sid_set = {player["sid"] for player in rank_data}
        sid_info = {}
        if sid_set:
            status_map = await plugin.fetch_player_statuses_batch(list(sid_set))
            for sid, info in status_map.items():
                sid_info[sid] = {
                    "name": info.get("name") or sid,
                    "avatar_url": info.get("avatarfull") or info.get("avatar"),
                }
        play_records = getattr(plugin, "play_records", {}) or {}
        for player in rank_data:
            sid = player["sid"]
            info = sid_info.get(sid, {})
            fallback = info.get("name", sid[-8:] if len(sid) >= 8 else sid)
            player["name"] = plugin._resolve_bind_name(sid, fallback)
            player["avatar_url"] = info.get("avatar_url")
            player["top_game_id"] = None
            games = player.get("games") or []
            if not games:
                continue
            player["top_game_id"] = lookup_top_game_id(
                play_records,
                ranking,
                sid,
                games[0]["name"],
                days,
                base_day_offset,
            )
            for game in games:
                gid = game.get("gameid")
                if not gid:
                    continue
                resolved = await plugin.get_chinese_game_name(str(gid), game.get("name"))
                if resolved:
                    game["name"] = resolved
        return rank_data

    async def collect_frames(self, rank_data: List[dict]) -> dict:
        plugin = self._plugin
        avatar_frame_paths = {}
        for player in rank_data:
            sid = player.get("sid", "")
            if not sid:
                continue
            frame_path = await get_avatar_frame_path(plugin.data_dir, sid, proxy=plugin.proxy)
            if not frame_path:
                frame_url = await get_avatar_frame_url(sid, proxy=plugin.proxy)
                if frame_url:
                    frame_path = await get_avatar_frame_path(
                        plugin.data_dir,
                        sid,
                        frame_url,
                        proxy=plugin.proxy,
                    )
            if frame_path:
                avatar_frame_paths[sid] = frame_path
        return avatar_frame_paths

    async def render_file(self, rank_data: List[dict], period_label: str) -> Optional[str]:
        plugin = self._plugin
        avatar_frame_paths = await self.collect_frames(rank_data)

        async def cover_fetcher(gameid):
            return await plugin.get_game_cover_url(gameid)

        font_path = resolve_font_path("NotoSansHans-Regular.otf")
        img_bytes = await render_rank_image(
            plugin.data_dir,
            rank_data,
            period_label,
            font_path=font_path,
            proxy=plugin.proxy,
            cover_fetcher=cover_fetcher,
            avatar_frame_paths=avatar_frame_paths,
        )
        if not img_bytes:
            return None
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(img_bytes)
            return tmp.name

    async def render_aggregated(
        self,
        *,
        days: int,
        period_label: str,
        group_id: Optional[str] = None,
        base_day_offset: int = 0,
    ) -> Optional[str]:
        plugin = self._plugin
        ranking = plugin.ranking_service
        rank_data = ranking.aggregate(
            days=days,
            sids=ranking.target_sids(group_id),
            base_day_offset=base_day_offset,
        )
        if not rank_data:
            return None
        await self.enrich(rank_data, days=days, base_day_offset=base_day_offset)
        tmp_path = await self.render_file(rank_data, period_label)
        if not tmp_path:
            raise RuntimeError("渲染图片失败")
        return tmp_path
