"""Configuration loader for the MT5 Grid Bot.

All tunables live in the .env file. This module parses them once into a
frozen dataclass so the rest of the bot can import a single ``CONFIG`` object.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)


def _req(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required env var '{key}'. Copy .env.example to .env and fill it in."
        )
    return val


def _f(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)).strip())


def _i(key: str, default: int) -> int:
    return int(float(os.getenv(key, str(default)).strip()))


def _b(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # credentials
    login: int
    password: str
    server: str
    terminal_path: str

    # symbol
    symbol: str

    # grid geometry
    spacing_usd: float
    grid_levels: int
    sl_divisor: float

    # anchor
    anchor_tf: str

    # sizing
    lot_size: float
    max_open_positions: int
    max_pending_orders: int

    # order behaviour
    magic: int
    deviation: int
    comment: str

    # range detector (chop guard)
    range_detect: bool
    range_window_sec: float
    range_band_mult: float

    # runtime
    poll_seconds: float
    dry_run: bool
    close_on_exit: bool
    log_level: str

    @property
    def tp_dist(self) -> float:
        return self.spacing_usd

    @property
    def sl_dist(self) -> float:
        return self.spacing_usd / self.sl_divisor


def load_config() -> Config:
    return Config(
        login=int(_req("MT5_LOGIN")),
        password=_req("MT5_PASSWORD"),
        server=_req("MT5_SERVER"),
        terminal_path=os.getenv("MT5_PATH", "").strip(),
        symbol=_req("SYMBOL"),
        spacing_usd=_f("GRID_SPACING_USD", 5.0),
        grid_levels=_i("GRID_LEVELS", 10),
        sl_divisor=_f("SL_DIVISOR", 2.0),
        anchor_tf=os.getenv("ANCHOR_TIMEFRAME", "10S").strip().upper(),
        lot_size=_f("LOT_SIZE", 0.01),
        max_open_positions=_i("MAX_OPEN_POSITIONS", 20),
        max_pending_orders=_i("MAX_PENDING_ORDERS", 0),
        magic=_i("MAGIC", 990012),
        deviation=_i("DEVIATION", 20),
        comment=os.getenv("ORDER_COMMENT", "GridV12").strip(),
        poll_seconds=_f("POLL_SECONDS", 1.0),
        range_detect=_b("RANGE_DETECT", True),
        range_window_sec=_f("RANGE_WINDOW_SEC", 60.0),
        range_band_mult=_f("RANGE_BAND_MULT", 1.0),
        dry_run=_b("DRY_RUN", True),
        close_on_exit=_b("CLOSE_ON_EXIT", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )


CONFIG = load_config()
