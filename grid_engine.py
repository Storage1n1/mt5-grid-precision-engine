"""Pure grid geometry — no MT5 dependency, fully unit-testable.

Implements the user's mechanic:

  * The grid is anchored on the current candle's OPEN price:
        levels = open ± i*dist   for i = 1..grids
    (anchored exactly at the open — NOT snapped to a round-dollar lattice.)

  * EVERY level can trade BOTH directions. Which side is armed right now depends
    on where price sits relative to the level (a broker can only rest a buy-stop
    ABOVE price and a sell-stop BELOW price):

        price BELOW level -> arm BUY  : TP = level + dist , SL = level - dist/2
        price ABOVE level -> arm SELL : TP = level - dist , SL = level + dist/2

    As price crosses a level upward the buy fires (long, targeting the next level
    up); as it crosses downward the sell fires (short, targeting the next level
    down). So over time each level trades both ways.

Notes on the backtest-only rules that do NOT apply live:
  * "zoom into LTF to fill precisely" — a live pending stop order already fills
    exactly at the level price.
  * "same-candle TP must survive / SL immediate" — the broker resolves TP/SL
    tick-by-tick, so there is no bar-fill ambiguity to guard against.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class GridLevel:
    side: Side
    index: int          # signed distance multiple: +i above open, -i below
    entry: float        # the lattice level (pending stop price)
    tp: float
    sl: float

    @property
    def tag(self) -> str:
        """Identity = side + absolute level price (one side armed per level)."""
        return f"{self.side.value}:{self.entry:g}"


def _order_for_level(level: float, price: float, idx: int,
                     tp_dist: float, sl_dist: float, digits: int) -> GridLevel | None:
    """Build the currently-armable order for a level given where price is."""
    L = round(level, digits)
    if price < L:
        return GridLevel(Side.BUY, idx, L,
                         round(L + tp_dist, digits), round(L - sl_dist, digits))
    if price > L:
        return GridLevel(Side.SELL, idx, L,
                         round(L - tp_dist, digits), round(L + sl_dist, digits))
    return None  # price sitting exactly on the level -> nothing armable


def build_grid(
    anchor_open: float,
    price: float,
    spacing: float,
    levels: int,
    tp_dist: float,
    sl_dist: float,
    digits: int = 2,
) -> list[GridLevel]:
    """Desired armed orders for the candle anchored at ``anchor_open``.

    Returns one GridLevel per lattice line (open ± i*spacing), each already
    resolved to the side that can be armed given the current ``price``.
    """
    if spacing <= 0:
        raise ValueError("spacing must be > 0")
    if levels < 1:
        raise ValueError("levels must be >= 1")

    out: list[GridLevel] = []
    for i in range(1, levels + 1):
        up = _order_for_level(anchor_open + i * spacing, price, i, tp_dist, sl_dist, digits)
        if up is not None:
            out.append(up)
        dn = _order_for_level(anchor_open - i * spacing, price, -i, tp_dist, sl_dist, digits)
        if dn is not None:
            out.append(dn)
    return out
