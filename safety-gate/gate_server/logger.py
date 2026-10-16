"""可定位性日志：轮转写本地文件 + 控制台。"""

from __future__ import annotations

import logging
import logging.handlers
import os

_LOGGER = "gate_server"


def setup(log_dir: str) -> None:
    logger = logging.getLogger(_LOGGER)
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "service.log"), maxBytes=10_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def log_request(tool_name: str, gate, latency_ms: float) -> None:
    logging.getLogger(_LOGGER).info(
        "tool=%s -> %s/%s risk_factors=%s latency=%.1fms",
        tool_name, gate.authorization.value, gate.risk_level.value, gate.risk_factors, latency_ms,
    )


def log_error(message: str) -> None:
    logging.getLogger(_LOGGER).error("%s", message)

