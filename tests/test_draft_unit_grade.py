from sports_aggregator.cfb.draft_matchups import _unit_dataset, _unit_grade


def test_each_player_counts_once_and_in_proportion_to_snaps():
    rows = [
        # One edge rusher appears in three datasets (as pff_matchup_rows returns him).
        {"position": "ED", "dataset": "defense", "primary_grade": 90.0, "usage_count": 700},
        {"position": "ED", "dataset": "pass_rush", "primary_grade": 90.0, "usage_count": 350},
        {"position": "ED", "dataset": "run_defense_detail", "primary_grade": 90.0, "usage_count": 300},
        {"position": "ED", "dataset": "defense", "primary_grade": 70.0, "usage_count": 690},
        {"position": "ED", "dataset": "defense", "primary_grade": 30.0, "usage_count": 20},  # reserve
    ]

    grade, counted = _unit_grade(rows, ("ED",))

    assert counted == 3
    assert round(grade, 3) == round((90 * 700 + 70 * 690 + 30 * 20) / (700 + 690 + 20), 3)


def test_the_dataset_follows_the_unit_not_the_first_row_found():
    assert _unit_dataset(("G", "C")) == "blocking"
    assert _unit_dataset(("G", "C", "TE")) == "blocking"
    assert _unit_dataset(("CB", "S")) == "defense"
    assert _unit_dataset(("WR", "TE")) == "receiving"


def test_no_graded_players_is_no_grade_and_zero_snaps_fall_back_to_a_plain_mean():
    assert _unit_grade([], ("ED",)) == (None, 0)
    rows = [{"position": "DI", "dataset": "defense", "primary_grade": 70.0, "usage_count": 0},
            {"position": "DI", "dataset": "defense", "primary_grade": 60.0, "usage_count": 0}]
    assert _unit_grade(rows, ("DI",)) == (65.0, 2)
