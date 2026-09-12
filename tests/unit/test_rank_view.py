import unittest
from datetime import datetime
from types import SimpleNamespace

from src.application.services.rank_view import (
    RankViewService,
    lookup_top_game_id,
    parse_rank_period,
)
from src.application.services.ranking import RankingService


class RankViewTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_rank_period(self):
        self.assertEqual((1, "今日"), parse_rank_period(""))
        self.assertEqual((7, "最近7天"), parse_rank_period("week"))
        self.assertEqual((30, "最近30天"), parse_rank_period("month"))
        self.assertEqual((3, "最近3天"), parse_rank_period("3"))
        self.assertEqual((1, "最近1天"), parse_rank_period("0"))

    def test_lookup_top_game_id_uses_offset_day(self):
        ranking = RankingService(now=lambda: datetime(2026, 9, 12, 12, 0))
        play_records = {
            "2026-09-11": {"s1": {"730": {"name": "CS2", "minutes": 10}}},
            "2026-09-12": {"s1": {"570": {"name": "Dota", "minutes": 20}}},
        }
        self.assertEqual(
            "730",
            lookup_top_game_id(play_records, ranking, "s1", "CS2", 1, -1),
        )
        self.assertEqual(
            "570",
            lookup_top_game_id(play_records, ranking, "s1", "Dota", 1, 0),
        )

    async def test_enrich_fills_bind_name_and_top_game(self):
        ranking = RankingService(now=lambda: datetime(2026, 9, 12, 12, 0))
        plugin = SimpleNamespace(
            ranking_service=ranking,
            play_records={
                "2026-09-12": {"s1": {"730": {"name": "CS2", "minutes": 40}}},
            },
            fetch_player_statuses_batch=None,
            get_chinese_game_name=None,
            _resolve_bind_name=lambda sid, name: f"bind-{sid}",
        )

        async def fetch(sids):
            return {"s1": {"name": "SteamName", "avatarfull": "http://a"}}

        async def zh(gameid, name):
            return "反恐精英2" if str(gameid) == "730" else name

        plugin.fetch_player_statuses_batch = fetch
        plugin.get_chinese_game_name = zh
        view = RankViewService(plugin)
        rows = [
            {
                "sid": "s1",
                "games": [{"name": "CS2", "minutes": 40, "gameid": "730"}],
            }
        ]
        await view.enrich(rows, days=1, base_day_offset=0)
        self.assertEqual("bind-s1", rows[0]["name"])
        self.assertEqual("http://a", rows[0]["avatar_url"])
        self.assertEqual("730", rows[0]["top_game_id"])
        self.assertEqual("反恐精英2", rows[0]["games"][0]["name"])
