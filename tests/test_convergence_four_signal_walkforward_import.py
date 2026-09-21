def test_convergence_four_signal_imports_walk_forward_helper():
    import sports_aggregator.cfb.convergence_four_signal as four

    assert callable(four.walk_forward_combined_scores)
