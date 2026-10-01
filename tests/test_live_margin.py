from sports_aggregator.nfl.live_margin import LiveMargin, UNAVAILABLE
from sports_aggregator.nfl.qb_player_ablation import QBTracker


class _Repo:
    def __init__(self, depth, injuries):
        self._depth, self._inj = depth, injuries

    def current_depth_chart(self, season, team):
        return self._depth

    def team_injuries(self, season, team):
        return self._inj


def _ctx(depth, injuries, last_qb=None):
    ctx = object.__new__(LiveMargin)
    ctx.repository, ctx.season, ctx.week = _Repo(depth, injuries), 2026, 4
    ctx.tracker = QBTracker()
    ctx.tracker.last_qb = last_qb or {}
    return ctx


def _qb(gsis, rank, name="X"):
    return {"position_abbreviation": "QB", "gsis_id": gsis, "position_rank": rank, "player_name": name}


def test_starter_is_depth_chart_qb1():
    ctx = _ctx([_qb("b", 2, "Backup"), _qb("a", 1, "Starter")], [])
    assert ctx.expected_starter("GB")["qb_id"] == "a"


def test_out_starter_falls_to_next_qb_but_questionable_does_not():
    depth = [_qb("a", 1), _qb("b", 2)]
    assert _ctx(depth, [{"gsis_id": "a", "designation": "O"}]).expected_starter("GB")["qb_id"] == "b"
    assert _ctx(depth, [{"gsis_id": "a", "designation": "Q"}]).expected_starter("GB")["qb_id"] == "a"
    assert "Q" not in UNAVAILABLE


def test_falls_back_to_last_game_passer_without_depth_chart():
    s = _ctx([], [], {"GNB": "z"}).expected_starter("GB")
    assert s == {"qb_id": "z", "name": None, "source": "last_game"}
    assert _ctx([], []).expected_starter("GB") is None


def test_change_flag_follows_expected_starter():
    ctx = _ctx([], [], {"GNB": "old"})
    assert ctx.tracker.features("GNB", "old", 2026)["changed"] == 0.0
    assert ctx.tracker.features("GNB", "new", 2026)["changed"] == 1.0
