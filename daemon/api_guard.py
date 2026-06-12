from clock import beijing_now


class ApiGuard:
    def __init__(self, limit_per_hour: int = 30):
        self._limit = limit_per_hour
        self._count = 0
        self._hour = -1

    def check(self) -> bool:
        current_hour = beijing_now().hour
        if current_hour != self._hour:
            self._count = 0
            self._hour = current_hour
        if self._count >= self._limit:
            return False
        self._count += 1
        return True

    @property
    def calls_this_hour(self) -> int:
        return self._count

    @property
    def limit(self) -> int:
        return self._limit
