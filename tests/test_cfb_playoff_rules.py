from sports_aggregator.cfb.playoff_rules import (
    CFPFormat, conference_order, play_bracket, select_field, title_game_pair,
)


def test_conference_order_uses_win_pct_then_head_to_head():
    games = [("A", "B", "A"), ("A", "C", "C"), ("B", "C", "B")]
    # all 1-1: mini-league is a 3-cycle (every team 1-1), so the fallback decides
    assert conference_order(["A", "B", "C"], games, {"A": 1, "B": 2, "C": 3}) == ["C", "B", "A"]
    # two-way tie broken by head-to-head even when the fallback disagrees
    games = [("A", "B", "B"), ("A", "C", "A"), ("B", "C", "C")]
    games += [("A", "D", "A"), ("B", "D", "D")]
    order = conference_order(["A", "B", "C", "D"], games, {"A": 9, "B": 0, "C": 0, "D": 0})
    assert order[0] in {"A", "B", "C", "D"} and len(order) == 4


def test_two_way_tie_head_to_head_beats_fallback():
    games = [("X", "Y", "Y"), ("X", "Z", "X"), ("Y", "Z", "Z")]
    # X 1-1, Y 1-1, Z 1-1 is a cycle; instead make X and Y tied at 2-1 with Y>X
    games = [("X", "Y", "Y"), ("X", "Z", "X"), ("X", "W", "X"), ("Y", "Z", "Y"),
             ("Y", "W", "Y"), ("Z", "W", "W")]
    order = conference_order(["X", "Y", "Z", "W"], games, {"X": 99, "Y": 0, "Z": 0, "W": 0})
    assert order[:2] == ["Y", "X"]


def test_title_game_pair_needs_two_teams():
    assert title_game_pair(["A"]) is None
    assert title_game_pair(["A", "B", "C"]) == ("A", "B")


def _ranking(n=20):
    return [f"T{i}" for i in range(1, n + 1)]


def _teams(field):
    return [row["team"] for row in field]


def test_power_four_champions_are_automatic_even_when_ranked_low():
    champs = {"T3": "SEC", "T9": "Big Ten", "T14": "ACC", "T18": "Big 12", "T7": "Sun Belt"}
    field = select_field(_ranking(), champs)
    assert len(field) == 12
    assert {"T3", "T9", "T14", "T18", "T7"} <= set(_teams(field))
    assert {row["team"] for row in field if row["auto_bid"]} == {"T3", "T9", "T14", "T18", "T7"}
    # 5 autos + 7 at-large: T1, T2, T4, T5, T6, T8, T10
    assert set(_teams(field)) == {"T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T14", "T18"}


def test_only_the_best_group_of_six_champion_gets_a_bid():
    champs = {"T2": "SEC", "T4": "Big Ten", "T5": "ACC", "T6": "Big 12",
              "T15": "Mountain West", "T11": "Sun Belt", "T13": "American Athletic"}
    field = select_field(_ranking(), champs)
    autos = {row["team"] for row in field if row["auto_bid"]}
    assert autos == {"T2", "T4", "T5", "T6", "T11"}   # T11 is the top-ranked G6 champion
    assert "T15" not in _teams(field) and "T13" not in _teams(field)


def test_pac_12_champion_counts_as_group_of_six():
    champs = {"T2": "SEC", "T4": "Big Ten", "T5": "ACC", "T6": "Big 12", "T12": "Pac-12", "T9": "Sun Belt"}
    autos = {row["team"] for row in select_field(_ranking(), champs) if row["auto_bid"]}
    assert "T9" in autos and "T12" not in autos


def test_notre_dame_is_automatic_only_when_ranked_in_the_top_twelve():
    champs = {"T2": "SEC", "T4": "Big Ten", "T5": "ACC", "T6": "Big 12", "T8": "Sun Belt"}
    ranking = _ranking()
    ranking[10], ranking[0] = "Notre Dame", "T11"     # ND ranked 11th: in
    field = select_field(ranking, champs, independents=["Notre Dame"])
    nd = next(r for r in field if r["team"] == "Notre Dame")
    assert nd["auto_bid"] and len(field) == 12
    assert sum(1 for r in field if not r["auto_bid"]) == 6   # only six at-large
    ranking = _ranking()
    ranking[12] = "Notre Dame"                          # ranked 13th: just a candidate
    field = select_field(ranking, champs, independents=["Notre Dame"])
    assert "Notre Dame" not in _teams(field)


def test_automatic_qualifier_outside_top_twelve_is_the_bottom_seed():
    champs = {"T20": "ACC", "T3": "SEC", "T5": "Big Ten", "T7": "Big 12", "T9": "Sun Belt"}
    field = select_field(_ranking(), champs)
    assert field[-1]["team"] == "T20" and field[-1]["seed"] == 12
    # at-large teams that are ranked ahead keep their order
    assert _teams(field)[:4] == ["T1", "T2", "T3", "T4"]


def test_top_four_by_rank_get_byes_without_being_champions():
    champs = {"T10": "SEC", "T11": "Big Ten", "T12": "ACC", "T13": "Big 12", "T14": "Sun Belt"}
    field = select_field(_ranking(), champs)
    assert [r["team"] for r in field if r["bye"]] == ["T1", "T2", "T3", "T4"]
    assert not any(r["auto_bid"] for r in field if r["bye"])


def test_legacy_five_best_champions_rule():
    fmt = CFPFormat(auto_rule="five_best_champions")
    champs = {"T3", "T7", "T9", "T14", "T15", "T16"}
    field = select_field(_ranking(), champs, fmt)
    assert _teams(field) == ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T14", "T15"]


def test_champion_byes_format():
    fmt = CFPFormat(auto_rule="five_best_champions", seeding="champion_byes")
    field = select_field(_ranking(), {"T3", "T7", "T9", "T14", "T15"}, fmt)
    assert _teams(field)[:4] == ["T3", "T7", "T9", "T14"]


def test_bracket_chalk_and_pairings():
    field = [{"seed": i, "team": f"S{i}"} for i in range(1, 13)]
    seen = []

    def win_prob(a, b, neutral):
        seen.append((a, b, neutral))
        return 1.0 if int(a[1:]) < int(b[1:]) else 0.0

    out = play_bracket(field, win_prob, lambda p: p >= 0.5)
    assert out["champion"] == "S1"
    assert seen[:4] == [("S5", "S12", False), ("S6", "S11", False), ("S7", "S10", False), ("S8", "S9", False)]
    assert [(a, b) for a, b, _ in seen[4:8]] == [("S1", "S8"), ("S4", "S5"), ("S2", "S7"), ("S3", "S6")]
    assert all(neutral for _, _, neutral in seen[4:])
    assert out["finalists"] == ["S1", "S2"]


def test_bracket_upset_path_matches_2025_shape():
    field = [{"seed": i, "team": f"S{i}"} for i in range(1, 13)]
    # 9 beats 8, 10 beats 7, 6 beats 11, 5 beats 12 -> 1v9, 4v5, 2v10, 3v6
    upsets = {("S8", "S9"), ("S7", "S10")}
    seen = []

    def win_prob(a, b, neutral):
        seen.append((a, b))
        return 0.0 if (a, b) in upsets else (1.0 if int(a[1:]) < int(b[1:]) else 0.0)

    play_bracket(field, win_prob, lambda p: p >= 0.5)
    assert [pair for pair in seen[4:8]] == [("S1", "S9"), ("S4", "S5"), ("S2", "S10"), ("S3", "S6")]
