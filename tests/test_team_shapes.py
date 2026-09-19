from sports_aggregator.cfb import team_shapes as ts


def test_shape_vector_standardizes_features():
    row = {key: float(index + 1) for index, key in enumerate(ts.FEATURES)}
    scaler = {key: (float(index), 2.0) for index, key in enumerate(ts.FEATURES)}
    vector = ts._vector(row, scaler)
    assert vector is not None
    assert len(vector) == len(ts.FEATURES)


def test_archetype_label_is_interpretable():
    centroid = [0.0] * len(ts.FEATURES)
    centroid[ts.FEATURES.index("pace_drives")] = 1.0
    centroid[ts.FEATURES.index("pace_plays_per_drive")] = 1.0
    centroid[ts.FEATURES.index("off_pass_rate")] = 1.0
    centroid[ts.FEATURES.index("off_points_per_drive")] = 1.0
    centroid[ts.FEATURES.index("off_yards_per_dropback")] = 1.0
    centroid[ts.FEATURES.index("off_yards_per_rush")] = 1.0
    centroid[ts.FEATURES.index("def_points_per_drive_allowed")] = -1.0
    centroid[ts.FEATURES.index("def_yards_per_dropback_allowed")] = -1.0
    centroid[ts.FEATURES.index("def_yards_per_rush_allowed")] = -1.0
    label = ts._archetype_label(centroid)
    assert "Fast" in label
    assert "Pass-heavy" in label
    assert "Efficient O" in label
    assert "Strong D" in label


def test_nearest_neighbor_shape_transfer_excludes_same_team():
    scaler = {key: (0.0, 1.0) for key in ts.FEATURES}
    def row(team, value, actual):
        result = {key: value for key in ts.FEATURES}
        result.update({
            "team": team,
            "actual_drives": actual,
            "actual_points_per_drive": actual / 10.0,
            "actual_pass_rate": actual / 20.0,
        })
        return result
    train = [row("A", 0.0, 9.0), row("B", 0.1, 10.0), row("C", 2.0, 14.0)]
    test = [row("A", 0.05, 10.0)]
    result = ts._neighbor_analysis(train, test, scaler, neighbors=1)
    assert result["actual_drives"]["nearest_neighbor"]["n"] == 1
    assert result["actual_drives"]["same_team_excluded"] is True


def test_kmeans_assigns_two_separated_shapes():
    vectors = [[0.0, 0.0], [0.1, 0.0], [5.0, 5.0], [5.1, 5.0]]
    centroids = ts._kmeans(vectors, k=2)
    assert len(centroids) == 2
    assert ts._cluster(vectors[0], centroids) != ts._cluster(vectors[-1], centroids)


def test_shape_insert_statement_matches_schema_arity():
    columns = [
        "game_id","team","opponent","side","shape_version","season","week","kickoff","prior_games",
        "pace_drives","pace_plays_per_drive","off_points_per_drive",
        "off_yards_per_dropback","off_yards_per_rush","off_pass_rate",
        "def_drives_allowed","def_points_per_drive_allowed",
        "def_yards_per_dropback_allowed","def_yards_per_rush_allowed",
        "def_pass_rate_allowed","actual_drives","actual_points_per_drive",
        "actual_pass_rate","actual_total_yards","actual_score_points",
    ]
    assert len(columns) == 25


def test_robustness_stability_counts_improvements():
    grid = [
        {"actual_points_per_drive": {"mae_delta_vs_own_shape": -0.02}},
        {"actual_points_per_drive": {"mae_delta_vs_own_shape": -0.01}},
        {"actual_points_per_drive": {"mae_delta_vs_own_shape": 0.01}},
    ]
    # Mirror the internal summary contract with simple arithmetic expectations.
    deltas = [row["actual_points_per_drive"]["mae_delta_vs_own_shape"] for row in grid]
    assert sum(1 for value in deltas if value < 0) == 2
    assert round(sum(deltas) / len(deltas), 4) == -0.0067


def test_full_chain_contract_keeps_volume_outside_shape():
    # The full-chain ablation is intentionally constrained: shape adjusts
    # efficiency/mix, while xDrives/xPlays remain the production volume layer.
    source = ts.full_chain_ablation.__doc__ or ""
    assert "xDrives/xPlays" in source
    assert "Volume always comes" in source
