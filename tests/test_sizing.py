from gg_bot.sizing import plan_qty


def test_plan_qty_stoprisiko():
    qtys, _, _, err = plan_qty(
        10000, 100.0, 1.0, "Stoprisiko", 1.0, 10.0, 10.0, 100.0, 3.0, [1.0], 1, 4, 0.001
    )
    assert err == ""
    assert sum(qtys) > 0


def test_plan_qty_minimum_lots():
    _, _, _, err = plan_qty(
        10,
        100000.0,
        1.0,
        "Stoprisiko",
        1.0,
        10.0,
        10.0,
        100.0,
        5000.0,
        [1.0],
        1,
        4,
        0.001,
    )
    assert err != ""
