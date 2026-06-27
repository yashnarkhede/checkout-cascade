"""Payment-service chaos flags: parse, validate, clamp. In-memory, single-process."""


class ChaosError(Exception):
    pass


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _parse_float(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ChaosError(f"value '{value}' is not a number")
    if v < 0:
        raise ChaosError("value must be >= 0")
    return max(lo, min(hi, v))


def _parse_bool(value):
    t = str(value).strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ChaosError(f"value '{value}' is not a boolean")


class Chaos:
    def __init__(self, db_slowdown_max: float):
        self.db_slowdown_max = db_slowdown_max
        self.db_slowdown = 0.0
        self.random_error_rate = 0.0
        self.memory_leak = False
        self.payment_failure = False

    def set(self, flag, value):
        if flag == "db_slowdown":
            self.db_slowdown = _parse_float(value, 0.0, self.db_slowdown_max)
            return self.db_slowdown
        if flag == "random_error_rate":
            self.random_error_rate = _parse_float(value, 0.0, 1.0)
            return self.random_error_rate
        if flag == "memory_leak":
            self.memory_leak = _parse_bool(value)
            return self.memory_leak
        if flag == "payment_failure":
            self.payment_failure = _parse_bool(value)
            return self.payment_failure
        raise ChaosError(f"unknown flag '{flag}'")

    def reset(self):
        self.db_slowdown = 0.0
        self.random_error_rate = 0.0
        self.memory_leak = False
        self.payment_failure = False

    def as_dict(self):
        return {
            "db_slowdown": self.db_slowdown,
            "random_error_rate": self.random_error_rate,
            "memory_leak": self.memory_leak,
            "payment_failure": self.payment_failure,
        }
