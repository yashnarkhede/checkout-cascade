"""Checkout-service chaos flags: own_latency + random_error_rate (independent of payment)."""


class ChaosError(Exception):
    pass


def _parse_float(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ChaosError(f"value '{value}' is not a number")
    if v < 0:
        raise ChaosError("value must be >= 0")
    return max(lo, min(hi, v))


class Chaos:
    def __init__(self, own_latency_max: float):
        self.own_latency_max = own_latency_max
        self.own_latency = 0.0
        self.random_error_rate = 0.0

    def set(self, flag, value):
        if flag == "own_latency":
            self.own_latency = _parse_float(value, 0.0, self.own_latency_max)
            return self.own_latency
        if flag == "random_error_rate":
            self.random_error_rate = _parse_float(value, 0.0, 1.0)
            return self.random_error_rate
        raise ChaosError(f"unknown flag '{flag}'")

    def reset(self):
        self.own_latency = 0.0
        self.random_error_rate = 0.0

    def as_dict(self):
        return {"own_latency": self.own_latency, "random_error_rate": self.random_error_rate}
