from sports_aggregator.cfb.ep_v2_rebuild_cli import parser as rebuild_parser
from sports_aggregator.cfb.expected_points_event import MIN_CELL
from sports_aggregator.cfb.expected_points_event_cli import parser as event_parser


def test_event_cli_exposes_event_aligned_validation():
    args = event_parser().parse_args([
        "validate", "--from-year", "2025", "--to-year", "2025",
        "--model-version", "candidate",
    ])

    assert args.command == "validate"
    assert args.from_year == 2025
    assert args.to_year == 2025
    assert args.model_version == "candidate"


def test_rebuild_defaults_to_expanded_history_and_tuned_shrinkage():
    args = rebuild_parser().parse_args([])

    assert args.fit_from_year == 2015
    assert args.score_from_year == 2015
    assert args.min_cell == MIN_CELL == 10
