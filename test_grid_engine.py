"""Unit tests for the pure grid geometry (no MT5 needed).

Run:  py -3.11 test_grid_engine.py
"""
from grid_engine import build_grid, Side


def test_levels_anchored_at_open():
    # open 100, dist 1, 3 grids -> levels 101/102/103 and 99/98/97
    grid = build_grid(anchor_open=100.0, price=100.0, spacing=1.0, levels=3,
                      tp_dist=1.0, sl_dist=0.5)
    entries = sorted(g.entry for g in grid)
    assert entries == [97.0, 98.0, 99.0, 101.0, 102.0, 103.0], entries


def test_side_depends_on_price():
    # price below a level -> BUY armed; above -> SELL armed
    grid = build_grid(anchor_open=100.0, price=100.0, spacing=1.0, levels=2,
                      tp_dist=1.0, sl_dist=0.5)
    by_entry = {g.entry: g for g in grid}
    assert by_entry[101.0].side is Side.BUY        # price 100 < 101
    assert by_entry[99.0].side is Side.SELL         # price 100 > 99


def test_tp_sl_geometry():
    grid = build_grid(anchor_open=100.0, price=100.0, spacing=1.0, levels=1,
                      tp_dist=1.0, sl_dist=0.5)
    buy = next(g for g in grid if g.side is Side.BUY)
    sell = next(g for g in grid if g.side is Side.SELL)
    # BUY at 101: TP next level up (102), SL mid (100.5)
    assert buy.entry == 101.0 and buy.tp == 102.0 and buy.sl == 100.5
    # SELL at 99: TP next level down (98), SL mid (99.5)
    assert sell.entry == 99.0 and sell.tp == 98.0 and sell.sl == 99.5


def test_anchored_at_open_not_snapped():
    # non-round open stays anchored exactly at the open (no lattice snapping)
    grid = build_grid(anchor_open=102.3, price=102.3, spacing=5.0, levels=1,
                      tp_dist=5.0, sl_dist=2.5)
    buy = next(g for g in grid if g.side is Side.BUY)
    sell = next(g for g in grid if g.side is Side.SELL)
    assert buy.entry == 107.3, buy.entry
    assert sell.entry == 97.3, sell.entry


def test_side_flips_as_price_crosses():
    # level 101: price 100 -> BUY (armed below); price 101.5 -> SELL (armed above)
    below = build_grid(100.0, 100.0, 1.0, 1, 1.0, 0.5)
    above = build_grid(100.0, 101.5, 1.0, 1, 1.0, 0.5)
    b = {g.entry: g for g in below}
    a = {g.entry: g for g in above}
    assert b[101.0].side is Side.BUY
    assert a[101.0].side is Side.SELL


def test_tags_unique():
    grid = build_grid(50000.0, 50000.0, 5.0, 10, 5.0, 2.5)
    tags = [g.tag for g in grid]
    assert len(tags) == len(set(tags)) == 20


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print("All tests passed.")
