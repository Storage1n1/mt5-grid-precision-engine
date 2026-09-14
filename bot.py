"""Grid strategy orchestration — candle-anchored, bidirectional, event-driven.

Each candle (ANCHOR_TIMEFRAME) opens a fresh grid anchored on that candle's open
price: levels = open ± i*dist for i = 1..grids. Every level can trade BOTH ways:

  * price below a level -> a BUY_STOP is armed there  (TP +dist, SL -dist/2)
  * price above a level -> a SELL_STOP is armed there (TP -dist, SL +dist/2)

So an upward cross of a level goes long toward the next level up, a downward
cross goes short toward the next level down.

Reconcile (each loop), with NO timer churn:
  * within a candle the open is constant, so the level prices are constant; the
    only changes are side-flips as price crosses a level (i.e. around fills).
  * at a new candle the open moves, so the grid is rebuilt around it.
  * a level that already holds an OPEN position is left alone (no same-level
    hedge, no re-arm) until that position closes — which is exactly
    "after SL, no re-entry until price comes back to the level".

Live vs backtest: pending stop orders fill exactly at the level, and the broker
resolves TP/SL tick-by-tick, so the LTF-precision and same-candle-survive rules
from the backtest spec are intrinsic here and need no special handling.
"""
from __future__ import annotations

import time

from config import Config
from grid_engine import GridLevel, build_grid
from mt5_client import MT5Client


def _order_tag(order, digits: int) -> str:
    """Identity of a broker order derived from its OWN fields, not the comment.

    Brokers (e.g. Exness) freely rewrite/strip order comments, so matching on the
    comment makes the bot fail to recognise its own pendings and re-place them
    every loop. MT5 order types are even for buys (0/2/4/6) and odd for sells
    (1/3/5/7), so side + price_open is a reliable, comment-independent key that
    matches GridLevel.tag.
    """
    side = "BUY" if order.type % 2 == 0 else "SELL"
    return f"{side}:{round(order.price_open, digits):g}"


class GridBot:
    def __init__(self, cfg: Config, client: MT5Client, logger):
        self.cfg = cfg
        self.client = client
        self.log = logger
        self._running = True
        self._last_open: float | None = None
        self._px_hist: list[tuple[float, float]] = []   # (timestamp, mid)
        self._ranging = False
        self._broker_full = False

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------ range detector
    def _range_window(self, mid: float) -> tuple[float, float] | None:
        """Return the established (lo, hi) price window, or None.

        Only the *shape* of recent price — we decide good-vs-bad per level later.
        Established = a FULL window's worth of samples is available.
        """
        if not self.cfg.range_detect:
            return None
        import time as _t
        now = _t.time()
        self._px_hist.append((now, mid))
        cutoff = now - self.cfg.range_window_sec
        self._px_hist = [(t, p) for (t, p) in self._px_hist if t >= cutoff]
        if len(self._px_hist) < 5:
            return None
        if now - self._px_hist[0][0] < self.cfg.range_window_sec * 0.9:
            return None  # not enough history to confirm a full window yet
        return (min(p for _, p in self._px_hist), max(p for _, p in self._px_hist))

    def _is_bad_range(self, level: float, window: tuple[float, float] | None) -> bool:
        """True if `level` is stuck in a LOSING range: price crosses it but never
        travels far enough to reach either take-profit — i.e. it only ever pays
        out SLs (the SL-SL / entry-SL zone). A range that DOES reach a TP (a
        'good' range) returns False, so those levels stay armed.
        """
        if window is None:
            return False
        lo, hi = window
        if not (lo <= level <= hi):
            return False  # price isn't even crossing this level
        reach = self.cfg.range_band_mult * self.cfg.tp_dist
        reached_up = (hi - level) >= reach        # buy could hit its TP
        reached_dn = (level - lo) >= reach        # sell could hit its TP
        return not (reached_up or reached_dn)      # bad only if NEITHER TP reachable

    # ------------------------------------------------------------------ reconcile
    def _reconcile(self) -> None:
        price = self.client.current_price()
        if price is None:
            return
        bid, ask = price
        mid = (bid + ask) / 2.0
        digits = self.client.meta.digits if self.client.meta else 2

        # STATIC grid: anchor once on the first tick we see, then freeze the
        # levels for the life of the session. Levels = anchor_open ± i*dist.
        if self._last_open is None:
            self._last_open = mid
            self.log.info("Static grid anchored ONCE @ %.2f (%d levels each side, dist=$%.2f)",
                          mid, self.cfg.grid_levels, self.cfg.spacing_usd)
        anchor_open = self._last_open

        grid = build_grid(
            anchor_open=anchor_open,
            price=mid,
            spacing=self.cfg.spacing_usd,
            levels=self.cfg.grid_levels,
            tp_dist=self.cfg.tp_dist,
            sl_dist=self.cfg.sl_dist,
            digits=digits,
        )
        desired = {lvl.tag: lvl for lvl in grid}

        # chop guard: suspend only the levels stuck in a LOSING (sub-TP) range
        window = self._range_window(mid)

        def _suspended(price: float) -> bool:
            return self._is_bad_range(price, window)

        existing = {_order_tag(o, digits): o for o in self.client.pending_orders()}
        positions = self.client.open_positions()
        # block any level price that already holds an open position (either side)
        busy_levels = {round(p.price_open, digits) for p in positions}

        # 1) cancel pendings that are no longer desired (side flipped / level gone)
        #    OR that now sit in a bad (losing) range — leave good ranges armed
        n_suspended = 0
        for tag, order in existing.items():
            if tag not in desired or _suspended(order.price_open):
                self.client.cancel_order(order.ticket)
                if _suspended(order.price_open):
                    n_suspended += 1

        was_ranging = self._ranging
        self._ranging = window is not None and any(_suspended(l.entry) for l in grid)
        if self._ranging and not was_ranging:
            self.log.info("Bad range detected — suspending sub-TP levels (window %.2f-%.2f).",
                          window[0], window[1])
        elif not self._ranging and was_ranging:
            self.log.info("Range cleared / broke out — re-arming levels.")

        # 2) arm desired orders that are missing, level not busy, within caps.
        #    desired is ordered nearest-level-first, so when a cap/limit is hit we
        #    keep the orders closest to price.
        n_positions = len(positions)
        n_pending = len(existing)
        cap_pending = self.cfg.max_pending_orders
        placed = 0
        hit_limit = False
        for tag, lvl in desired.items():
            if tag in existing:
                continue
            if lvl.entry in busy_levels:
                continue
            if _suspended(lvl.entry):
                continue  # suspended: this level is stuck in a losing range
            if n_positions + placed >= self.cfg.max_open_positions:
                break
            if cap_pending > 0 and n_pending + placed >= cap_pending:
                break  # self-imposed pending cap reached
            if not self._stop_is_valid(lvl, bid, ask):
                # price is inside the bid/ask band around the level — can't rest
                # a stop yet; it'll be armed once price clears the level.
                continue
            status = self.client.place_stop(lvl.side, lvl.entry, lvl.tp, lvl.sl, lvl.tag)
            if status == "ok":
                placed += 1
            elif status == "limit":
                hit_limit = True
                break  # broker pending-order cap — stop trying this cycle

        if hit_limit and not self._broker_full:
            self._broker_full = True
            self.log.warning(
                "Broker pending-order limit reached (%d resting). Not placing more. "
                "Lower GRID_LEVELS or set MAX_PENDING_ORDERS below the broker cap.",
                n_pending)
        elif not hit_limit and self._broker_full:
            self._broker_full = False
            self.log.info("Broker order slots available again.")

    @staticmethod
    def _stop_is_valid(lvl: GridLevel, bid: float, ask: float) -> bool:
        """A BUY_STOP must sit above ask; a SELL_STOP below bid."""
        if lvl.side.name == "BUY":
            return lvl.entry > ask
        return lvl.entry < bid

    # --------------------------------------------------------------------- loop
    def run(self) -> None:
        self.log.info(
            "Grid bot starting | symbol=%s dist=$%.2f grids=%d tp=$%.2f sl=$%.2f anchor=%s dry_run=%s",
            self.cfg.symbol, self.cfg.spacing_usd, self.cfg.grid_levels,
            self.cfg.tp_dist, self.cfg.sl_dist, self.cfg.anchor_tf, self.cfg.dry_run,
        )
        while self._running:
            try:
                if not self.client.ensure_connected():
                    self.log.error("No connection; retrying in 10s")
                    time.sleep(10)
                    continue

                self._reconcile()
                time.sleep(self.cfg.poll_seconds)

            except KeyboardInterrupt:
                self.log.info("KeyboardInterrupt — stopping.")
                break
            except Exception as e:  # noqa: BLE001
                self.log.exception("Loop error: %s", e)
                time.sleep(self.cfg.poll_seconds)

        self.log.info("Grid bot loop ended.")
