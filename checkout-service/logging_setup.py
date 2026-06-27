"""Structured one-line stdout logger. Identical to payment-service/logging_setup.py."""
import sys
from metrics import now_iso


def make_logger(service: str):
    def log(event: str, level: str = "INFO", **fields):
        parts = [f"{now_iso()} {level:<5} {service} event={event}"]
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        print(" ".join(parts), file=sys.stdout, flush=True)
    return log
