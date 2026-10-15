"""Recent frame-rate measurement for the live viewer."""

from collections import deque


class RecentFrameRate:
    """Measure FPS over a sliding window instead of the whole session."""

    def __init__(self, window_seconds: float = 5.0) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def tick(self, timestamp: float) -> float:
        self._timestamps.append(timestamp)
        cutoff = timestamp - self.window_seconds
        while len(self._timestamps) > 1 and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) < 2:
            return 0.0
        elapsed = self._timestamps[-1] - self._timestamps[0]
        if elapsed <= 0:
            return 0.0
        return (len(self._timestamps) - 1) / elapsed
