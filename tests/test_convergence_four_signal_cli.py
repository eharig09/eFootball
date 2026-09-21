from pathlib import Path

from sports_aggregator.cfb import convergence_four_signal_cli as cli


def test_default_research_output_path_is_under_repo_root():
    path = cli._default_output_path(2025)
    assert path == (
        cli.REPO_ROOT
        / "research_outputs"
        / "cfb_convergence_four_signal_through_2025.json"
    )
    assert isinstance(path, Path)
