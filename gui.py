"""Tkinter dashboard for the MT5 Grid Bot (no external deps).

Shows account/equity, live price + anchor, the computed grid, working pending
orders, open positions and realized stats. Lets you tweak the core grid params,
toggle DRY_RUN, and Start/Stop the bot — all from one window.

Run:  py -3.11 gui.py     (or double-click gui.bat)
"""
from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from config import CONFIG, Config
from logger import get_logger
from mt5_client import MT5Client
from bot import GridBot
from grid_engine import build_grid


# ---------------------------------------------------------------- log -> GUI bridge
class QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                             datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


class GridBotGUI:
    REFRESH_MS = 1000

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MT5 Grid Bot — Precision Grid Engine V12")
        self.root.geometry("1040x720")

        self.log_q: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self.log = get_logger("gridbot", CONFIG.log_level)
        self.log.addHandler(QueueHandler(self.log_q))

        self.client: MT5Client | None = None
        self.bot: GridBot | None = None
        self.bot_thread: threading.Thread | None = None
        self.session_start = int(time.time())

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump_logs()
        self._refresh()

    # ------------------------------------------------------------------ UI build
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        # --- config / controls ---
        cfgf = ttk.LabelFrame(top, text="Settings", padding=8)
        cfgf.pack(side="left", fill="y")

        self.vars = {
            "symbol": tk.StringVar(value=CONFIG.symbol),
            "spacing": tk.StringVar(value=str(CONFIG.spacing_usd)),
            "levels": tk.StringVar(value=str(CONFIG.grid_levels)),
            "sl_divisor": tk.StringVar(value=str(CONFIG.sl_divisor)),
            "lot": tk.StringVar(value=str(CONFIG.lot_size)),
            "anchor": tk.StringVar(value=CONFIG.anchor_tf),
            "max_pos": tk.StringVar(value=str(CONFIG.max_open_positions)),
        }
        rows = [
            ("Symbol", "symbol"), ("Spacing $", "spacing"), ("Levels", "levels"),
            ("SL divisor", "sl_divisor"), ("Lot", "lot"), ("Anchor TF", "anchor"),
            ("Max positions", "max_pos"),
        ]
        for r, (label, key) in enumerate(rows):
            ttk.Label(cfgf, text=label).grid(row=r, column=0, sticky="w", pady=1)
            ttk.Entry(cfgf, textvariable=self.vars[key], width=12).grid(row=r, column=1, pady=1)

        self.dry_run = tk.BooleanVar(value=CONFIG.dry_run)
        ttk.Checkbutton(cfgf, text="DRY RUN (no real orders)", variable=self.dry_run).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 1))

        self.close_on_exit = tk.BooleanVar(value=CONFIG.close_on_exit)
        ttk.Checkbutton(cfgf, text="Close all on Stop/Exit", variable=self.close_on_exit).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self.btn_start = ttk.Button(cfgf, text="▶ Start", command=self.start_bot)
        self.btn_start.grid(row=len(rows) + 2, column=0, sticky="we", pady=2)
        self.btn_stop = ttk.Button(cfgf, text="■ Stop", command=self.stop_bot, state="disabled")
        self.btn_stop.grid(row=len(rows) + 2, column=1, sticky="we", pady=2)

        self.btn_flat = ttk.Button(cfgf, text="⚠ Flatten Now", command=self.flatten_now,
                                   state="disabled")
        self.btn_flat.grid(row=len(rows) + 3, column=0, columnspan=2, sticky="we", pady=2)

        # --- live status panel ---
        statf = ttk.LabelFrame(top, text="Status", padding=8)
        statf.pack(side="left", fill="both", expand=True, padx=(8, 0))

        self.status = {k: tk.StringVar(value="—") for k in
                       ("conn", "account", "balance", "equity", "float", "bid", "ask",
                        "anchor", "wins", "losses", "net", "open", "pend", "range")}
        grid_items = [
            ("Connection", "conn"), ("Account", "account"),
            ("Balance", "balance"), ("Equity", "equity"),
            ("Floating PnL", "float"), ("Realized net", "net"),
            ("Bid", "bid"), ("Ask", "ask"),
            ("Mid price", "anchor"), ("Wins / Losses", "wins"),
            ("Open positions", "open"), ("Pending orders", "pend"),
            ("Range guard", "range"),
        ]
        for i, (label, key) in enumerate(grid_items):
            r, c = divmod(i, 2)
            ttk.Label(statf, text=label + ":").grid(row=r, column=c * 2, sticky="w", padx=4, pady=2)
            ttk.Label(statf, textvariable=self.status[key], font=("Segoe UI", 9, "bold")).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=4, pady=2)

        # --- tables ---
        mid = ttk.Frame(self.root, padding=(8, 0))
        mid.pack(fill="both", expand=True)

        self.grid_tree = self._make_tree(mid, "Computed Grid",
                                         ("side", "idx", "entry", "tp", "sl", "state"))
        self.pos_tree = self._make_tree(mid, "Open Positions",
                                        ("ticket", "type", "vol", "price", "tp", "sl", "pnl"))

        # --- log pane ---
        logf = ttk.LabelFrame(self.root, text="Log", padding=4)
        logf.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_text = tk.Text(logf, height=10, bg="#131722", fg="#d1d4dc",
                                font=("Consolas", 9), wrap="none")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=sb.set)

    def _make_tree(self, parent, title, cols):
        f = ttk.LabelFrame(parent, text=title, padding=4)
        f.pack(side="left", fill="both", expand=True, padx=2)
        tree = ttk.Treeview(f, columns=cols, show="headings", height=10)
        for c in cols:
            tree.heading(c, text=c.upper())
            tree.column(c, width=70, anchor="center")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(f, command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.config(yscrollcommand=sb.set)
        return tree

    # ------------------------------------------------------------------ bot control
    def _build_config(self) -> Config:
        return dataclasses.replace(
            CONFIG,
            symbol=self.vars["symbol"].get().strip(),
            spacing_usd=float(self.vars["spacing"].get()),
            grid_levels=int(float(self.vars["levels"].get())),
            sl_divisor=float(self.vars["sl_divisor"].get()),
            lot_size=float(self.vars["lot"].get()),
            anchor_tf=self.vars["anchor"].get().strip().upper(),
            max_open_positions=int(float(self.vars["max_pos"].get())),
            dry_run=self.dry_run.get(),
            close_on_exit=self.close_on_exit.get(),
        )

    def start_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            return
        try:
            cfg = self._build_config()
        except ValueError as e:
            self.log.error("Bad settings: %s", e)
            return

        self.client = MT5Client(cfg, self.log)
        try:
            self.client.connect()
        except Exception as e:  # noqa: BLE001
            self.log.error("Connect failed: %s", e)
            self.client = None
            return

        self.session_start = int(time.time())
        self.bot = GridBot(cfg, self.client, self.log)
        self.bot_thread = threading.Thread(target=self.bot.run, daemon=True)
        self.bot_thread.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_flat.config(state="normal")

    def stop_bot(self):
        if self.bot:
            self.bot.stop()
        self.btn_stop.config(state="disabled")
        self.btn_flat.config(state="disabled")
        # disconnect after the loop winds down
        def _cleanup():
            if self.bot_thread:
                self.bot_thread.join(timeout=8)
            if self.client:
                if self.client.cfg.close_on_exit:
                    self.log.info("Close-on-Stop enabled — flattening.")
                    try:
                        self.client.flatten_all()
                    except Exception as e:  # noqa: BLE001
                        self.log.error("flatten failed: %s", e)
                self.client.shutdown()
                self.client = None
            self.btn_start.config(state="normal")
        threading.Thread(target=_cleanup, daemon=True).start()

    def flatten_now(self):
        """Manual panic button — cancel all pendings and close all positions."""
        if not self.client:
            return
        self.log.warning("Manual FLATTEN requested.")
        threading.Thread(target=self.client.flatten_all, daemon=True).start()

    def on_close(self):
        """Window X: flatten (if enabled) then exit cleanly."""
        if self.client:
            self.stop_bot()
            self.root.after(1500, self.root.destroy)
        else:
            self.root.destroy()

    # ------------------------------------------------------------------ refresh loop
    def _pump_logs(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                if int(self.log_text.index("end-1c").split(".")[0]) > 1000:
                    self.log_text.delete("1.0", "200.0")
        except queue.Empty:
            pass
        self.root.after(200, self._pump_logs)

    def _refresh(self):
        try:
            self._update_status()
        except Exception as e:  # noqa: BLE001
            self.status["conn"].set(f"error: {e}")
        self.root.after(self.REFRESH_MS, self._refresh)

    def _update_status(self):
        c = self.client
        if c is None:
            self.status["conn"].set("disconnected")
            return
        self.status["conn"].set("● LIVE" if not c.cfg.dry_run else "● DRY RUN")

        acct = c.account_snapshot()
        if acct:
            self.status["account"].set(f"{acct['login']} ({acct['currency']})")
            self.status["balance"].set(f"{acct['balance']:.2f}")
            self.status["equity"].set(f"{acct['equity']:.2f}")
            self.status["float"].set(f"{acct['profit']:.2f}")

        price = c.current_price()
        mid = None
        if price:
            self.status["bid"].set(f"{price[0]:.2f}")
            self.status["ask"].set(f"{price[1]:.2f}")
            mid = (price[0] + price[1]) / 2.0

        positions = c.open_positions()
        pendings = c.pending_orders()
        self.status["anchor"].set(f"{mid:.2f}" if mid else "—")
        self.status["open"].set(str(len(positions)))
        self.status["pend"].set(str(len(pendings)))

        stats = c.realized_stats(self.session_start)
        self.status["wins"].set(f"{stats['wins']} / {stats['losses']}")
        self.status["net"].set(f"{stats['net']:.2f}")
        self.status["range"].set("● SUSPENDED" if getattr(self.bot, "_ranging", False) else "clear")

        # grid table — STATIC levels (the bot's frozen anchor), side armed at price
        anchor_open = getattr(self.bot, "_last_open", None) if self.bot else None
        if mid:
            digits = c.meta.digits if c.meta else 2
            grid = build_grid(anchor_open or mid, mid, c.cfg.spacing_usd,
                              c.cfg.grid_levels, c.cfg.tp_dist, c.cfg.sl_dist, digits)
            pos_prices = {round(p.price_open, digits) for p in positions}
            pend_prices = {round(o.price_open, digits) for o in pendings}
            self.grid_tree.delete(*self.grid_tree.get_children())
            for g in grid:
                p = round(g.entry, digits)
                state = "FILLED" if p in pos_prices else ("WORKING" if p in pend_prices else "idle")
                self.grid_tree.insert("", "end", values=(
                    g.side.value, g.index, g.entry, g.tp, g.sl, state))

        # positions table
        self.pos_tree.delete(*self.pos_tree.get_children())
        for p in positions:
            ptype = "BUY" if p.type == 0 else "SELL"
            self.pos_tree.insert("", "end", values=(
                p.ticket, ptype, p.volume, round(p.price_open, 2),
                round(p.tp, 2), round(p.sl, 2), round(p.profit, 2)))


def main():
    root = tk.Tk()
    GridBotGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
