"""Thin, robust wrapper around the MetaTrader5 package.

Responsibilities:
  * connect / reconnect / shutdown
  * symbol metadata (digits, point, fill/expiration modes, min lot)
  * fetch the current anchor candle (for grid anchoring)
  * list / place / cancel orders scoped to this bot's MAGIC number
All broker calls funnel through here so the strategy stays broker-agnostic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import MetaTrader5 as mt5

from config import Config
from grid_engine import Side

# anchor-timeframe string -> mt5 timeframe constant
_TF_MAP = {
    "10S": getattr(mt5, "TIMEFRAME_M1", 1),  # sub-minute not exposed; see note below
    "30S": getattr(mt5, "TIMEFRAME_M1", 1),
    "1M": mt5.TIMEFRAME_M1,
    "5M": mt5.TIMEFRAME_M5,
    "15M": mt5.TIMEFRAME_M15,
    "30M": mt5.TIMEFRAME_M30,
    "1H": mt5.TIMEFRAME_H1,
    "4H": mt5.TIMEFRAME_H4,
}


@dataclass
class SymbolMeta:
    name: str
    digits: int
    point: float
    volume_min: float
    volume_step: float
    trade_tick_value: float


class MT5Client:
    def __init__(self, cfg: Config, logger):
        self.cfg = cfg
        self.log = logger
        self.meta: SymbolMeta | None = None

    # ---------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        kwargs = {
            "login": self.cfg.login,
            "password": self.cfg.password,
            "server": self.cfg.server,
        }
        if self.cfg.terminal_path:
            kwargs["path"] = self.cfg.terminal_path

        if not mt5.initialize(**kwargs):
            raise ConnectionError(f"mt5.initialize failed: {mt5.last_error()}")

        info = mt5.account_info()
        if info is None:
            raise ConnectionError(f"account_info failed: {mt5.last_error()}")
        self.log.info(
            "Connected: account=%s server=%s balance=%.2f %s",
            info.login, info.server, info.balance, info.currency,
        )

        if not mt5.symbol_select(self.cfg.symbol, True):
            raise ConnectionError(f"symbol_select({self.cfg.symbol}) failed: {mt5.last_error()}")

        si = mt5.symbol_info(self.cfg.symbol)
        if si is None:
            raise ConnectionError(f"symbol_info({self.cfg.symbol}) is None")
        self.meta = SymbolMeta(
            name=si.name,
            digits=si.digits,
            point=si.point,
            volume_min=si.volume_min,
            volume_step=si.volume_step,
            trade_tick_value=si.trade_tick_value,
        )
        self.log.info("Symbol %s ready (digits=%d point=%g)", si.name, si.digits, si.point)

    def ensure_connected(self) -> bool:
        """Return True if alive, otherwise attempt one reconnect."""
        if mt5.terminal_info() is not None and mt5.account_info() is not None:
            return True
        self.log.warning("Connection lost — attempting reconnect...")
        try:
            mt5.shutdown()
        except Exception:
            pass
        for attempt in range(1, 6):
            try:
                self.connect()
                return True
            except Exception as e:  # noqa: BLE001
                self.log.error("Reconnect %d/5 failed: %s", attempt, e)
                time.sleep(min(2 * attempt, 10))
        return False

    def shutdown(self) -> None:
        try:
            mt5.shutdown()
        finally:
            self.log.info("MT5 shut down.")

    # ----------------------------------------------------------------- market data
    def anchor_open(self) -> tuple[float, int] | None:
        """Return (open_price, bar_time) of the current anchor candle.

        MT5 does not expose sub-minute rates, so 10S/30S anchors fall back to
        the latest *tick* price as the anchor (re-anchored every POLL_SECONDS),
        which is the closest live-trading equivalent of TradingView's 1S replay.
        """
        tf_key = self.cfg.anchor_tf
        if tf_key in ("10S", "30S"):
            tick = mt5.symbol_info_tick(self.cfg.symbol)
            if tick is None:
                return None
            # quantise time to the anchor window so we only re-anchor per window
            window = 10 if tf_key == "10S" else 30
            bar_time = (tick.time // window) * window
            return float(tick.bid), int(bar_time)

        tf = _TF_MAP.get(tf_key, mt5.TIMEFRAME_M1)
        rates = mt5.copy_rates_from_pos(self.cfg.symbol, tf, 0, 1)
        if rates is None or len(rates) == 0:
            return None
        return float(rates[0]["open"]), int(rates[0]["time"])

    def current_price(self) -> tuple[float, float] | None:
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        if tick is None:
            return None
        return float(tick.bid), float(tick.ask)

    # ----------------------------------------------------------------- orders
    def open_positions(self) -> list:
        pos = mt5.positions_get(symbol=self.cfg.symbol) or []
        return [p for p in pos if p.magic == self.cfg.magic]

    def pending_orders(self) -> list:
        orders = mt5.orders_get(symbol=self.cfg.symbol) or []
        return [o for o in orders if o.magic == self.cfg.magic]

    def _normalize_volume(self, lot: float) -> float:
        m = self.meta
        step = m.volume_step or 0.01
        lot = max(lot, m.volume_min)
        return round(round(lot / step) * step, 8)

    def place_stop(self, side: Side, price: float, tp: float, sl: float, tag: str) -> str:
        """Place a BUY_STOP / SELL_STOP pending order.

        Returns: "ok" | "limit" (broker pending-order cap hit) | "fail".
        """
        if self.cfg.dry_run:
            self.log.info("[DRY_RUN] %s_STOP %s @ %.2f tp=%.2f sl=%.2f (%s)",
                          side.value, self.cfg.symbol, price, tp, sl, tag)
            return "ok"

        order_type = mt5.ORDER_TYPE_BUY_STOP if side is Side.BUY else mt5.ORDER_TYPE_SELL_STOP
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.cfg.symbol,
            "volume": self._normalize_volume(self.cfg.lot_size),
            "type": order_type,
            "price": price,
            "tp": tp,
            "sl": sl,
            "deviation": self.cfg.deviation,
            "magic": self.cfg.magic,
            "comment": f"{self.cfg.comment}|{tag}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            rc = getattr(result, "retcode", "None")
            cm = getattr(result, "comment", mt5.last_error())
            if rc == getattr(mt5, "TRADE_RETCODE_LIMIT_ORDERS", 10033) or rc == 10033:
                return "limit"   # broker pending-order cap; caller throttles
            self.log.error("place_stop %s @ %.2f failed: retcode=%s %s", side.value, price, rc, cm)
            return "fail"
        self.log.info("Placed %s_STOP @ %.2f tp=%.2f sl=%.2f (%s) ticket=%s",
                      side.value, price, tp, sl, tag, result.order)
        return "ok"

    # ----------------------------------------------------------------- account / stats
    def account_snapshot(self) -> dict | None:
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "login": info.login,
            "balance": info.balance,
            "equity": info.equity,
            "profit": info.profit,        # floating PnL of open positions
            "margin": info.margin,
            "free_margin": info.margin_free,
            "currency": info.currency,
        }

    def realized_stats(self, from_ts: int) -> dict:
        """Wins/losses/net realized PnL for this bot's deals since ``from_ts``."""
        import datetime as _dt
        deals = mt5.history_deals_get(
            _dt.datetime.fromtimestamp(from_ts), _dt.datetime.now()
        ) or []
        wins = losses = 0
        net = 0.0
        for d in deals:
            if d.magic != self.cfg.magic or d.symbol != self.cfg.symbol:
                continue
            if d.entry != mt5.DEAL_ENTRY_OUT:   # only count position closes
                continue
            pnl = d.profit + d.commission + d.swap
            net += pnl
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
        return {"wins": wins, "losses": losses, "net": net}

    def cancel_order(self, ticket: int) -> bool:
        if self.cfg.dry_run:
            self.log.info("[DRY_RUN] cancel order %s", ticket)
            return True
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            self.log.error("cancel %s failed: %s", ticket, getattr(result, "comment", mt5.last_error()))
        return ok

    def close_position(self, pos) -> bool:
        """Close an open position with an opposing market order."""
        if self.cfg.dry_run:
            self.log.info("[DRY_RUN] close position %s (%.2f lots)", pos.ticket, pos.volume)
            return True
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        if tick is None:
            self.log.error("close_position %s: no tick", pos.ticket)
            return False
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.cfg.symbol,
            "position": pos.ticket,
            "volume": pos.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.cfg.deviation,
            "magic": self.cfg.magic,
            "comment": f"{self.cfg.comment}|close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        }
        result = mt5.order_send(request)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            self.log.info("Closed position %s", pos.ticket)
        else:
            self.log.error("close %s failed: %s", pos.ticket,
                           getattr(result, "comment", mt5.last_error()))
        return ok

    def cancel_all_pending(self) -> int:
        n = 0
        for o in self.pending_orders():
            if self.cancel_order(o.ticket):
                n += 1
        return n

    def flatten_all(self) -> tuple[int, int]:
        """Cancel every pending and close every open position for this bot.

        Returns (orders_cancelled, positions_closed).
        """
        cancelled = self.cancel_all_pending()
        closed = 0
        for p in self.open_positions():
            if self.close_position(p):
                closed += 1
        self.log.info("Flatten: cancelled %d orders, closed %d positions", cancelled, closed)
        return cancelled, closed

    def _filling_mode(self):
        si = mt5.symbol_info(self.cfg.symbol)
        mode = getattr(si, "filling_mode", 0)
        # prefer the broker-advertised mode; fall back to RETURN for pendings
        if mode & 1:  # FOK
            return mt5.ORDER_FILLING_FOK
        if mode & 2:  # IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN
