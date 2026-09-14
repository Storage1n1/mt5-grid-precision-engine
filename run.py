"""Entry point: wire config -> client -> bot and run with clean shutdown."""
from __future__ import annotations

import signal
import sys

from config import CONFIG
from logger import get_logger
from mt5_client import MT5Client
from bot import GridBot


def main() -> int:
    log = get_logger("gridbot", CONFIG.log_level)
    client = MT5Client(CONFIG, log)

    try:
        client.connect()
    except Exception as e:  # noqa: BLE001
        log.error("Startup failed: %s", e)
        return 1

    bot = GridBot(CONFIG, client, log)

    def _handle_sig(signum, _frame):
        log.info("Signal %s received — shutting down.", signum)
        bot.stop()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        bot.run()
    finally:
        if CONFIG.close_on_exit:
            log.info("CLOSE_ON_EXIT enabled — flattening all orders and positions.")
            try:
                client.flatten_all()
            except Exception as e:  # noqa: BLE001
                log.error("flatten_all failed: %s", e)
        client.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
