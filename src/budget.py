import time
import threading
from typing import Dict, Optional, Any

class BudgetExceededError(Exception):
    """Raised when runtime or request budget is exceeded."""
    pass

class CircuitBreakerError(Exception):
    """Raised when consecutive 5xx errors trip the circuit breaker."""
    pass

class RequestBudget:
    """
    Thread-safe budget controller for all network operations.
    Enforces request limits, wall-clock deadlines, stop flags, and circuit breaker.
    """
    def __init__(
        self,
        max_requests: Optional[int] = None,
        max_runtime_seconds: Optional[float] = None,
        consecutive_5xx_limit: int = 3,
        start_time: Optional[float] = None
    ):
        self.max_requests = max_requests
        self.max_runtime_seconds = max_runtime_seconds
        self.consecutive_5xx_limit = consecutive_5xx_limit
        self.start_time = start_time if start_time is not None else time.time()
        self.deadline = (self.start_time + max_runtime_seconds) if max_runtime_seconds is not None else None

        self._lock = threading.Lock()
        self.total_requests = 0
        self.consecutive_5xx = 0
        self.stop_requested = False
        self.stop_reason = ""

        self.requests_by_type: Dict[str, int] = {
            "metadata_coarse": 0,
            "metadata_exact": 0,
            "horizons_coarse": 0,
            "horizons_exact": 0,
            "download": 0,
            "other": 0
        }
        self.errors_by_type: Dict[str, int] = {}

    def request_stop(self, reason: str = "Stop requested"):
        with self._lock:
            self.stop_requested = True
            self.stop_reason = reason

    def check_budget(self):
        """
        Check if any budget constraint or stop condition is violated.
        Must be called before every remote call and within streaming loops.
        """
        with self._lock:
            if self.stop_requested:
                raise BudgetExceededError(f"Stop flag active: {self.stop_reason}")

            if self.deadline is not None and time.time() >= self.deadline:
                self.stop_requested = True
                self.stop_reason = f"Wall-clock deadline of {self.max_runtime_seconds}s exceeded"
                raise BudgetExceededError(self.stop_reason)

            if self.max_requests is not None and self.total_requests >= self.max_requests:
                self.stop_requested = True
                self.stop_reason = f"Request budget of {self.max_requests} exceeded"
                raise BudgetExceededError(self.stop_reason)

            if self.consecutive_5xx >= self.consecutive_5xx_limit:
                self.stop_requested = True
                self.stop_reason = f"Circuit breaker tripped after {self.consecutive_5xx} consecutive 5xx responses"
                raise CircuitBreakerError(self.stop_reason)

    def record_attempt(self, req_type: str = "other"):
        """
        Record a request attempt (including retries).
        Checks budget first, then atomically increments counters.
        """
        with self._lock:
            if self.stop_requested:
                raise BudgetExceededError(f"Stop flag active: {self.stop_reason}")
            if self.deadline is not None and time.time() >= self.deadline:
                self.stop_requested = True
                self.stop_reason = f"Wall-clock deadline of {self.max_runtime_seconds}s exceeded"
                raise BudgetExceededError(self.stop_reason)
            if self.max_requests is not None and self.total_requests >= self.max_requests:
                self.stop_requested = True
                self.stop_reason = f"Request budget of {self.max_requests} exceeded"
                raise BudgetExceededError(self.stop_reason)
            if self.consecutive_5xx >= self.consecutive_5xx_limit:
                self.stop_requested = True
                self.stop_reason = f"Circuit breaker tripped after {self.consecutive_5xx} consecutive 5xx responses"
                raise CircuitBreakerError(self.stop_reason)

            self.total_requests += 1
            if req_type in self.requests_by_type:
                self.requests_by_type[req_type] += 1
            else:
                self.requests_by_type[req_type] = 1

    def record_success(self):
        """Reset consecutive 5xx count on successful request."""
        with self._lock:
            self.consecutive_5xx = 0

    def record_5xx(self, status_code: int):
        """Record a 5xx response and trip circuit breaker if limit reached."""
        with self._lock:
            self.consecutive_5xx += 1
            err_key = f"HTTP_{status_code}"
            self.errors_by_type[err_key] = self.errors_by_type.get(err_key, 0) + 1
            if self.consecutive_5xx >= self.consecutive_5xx_limit:
                self.stop_requested = True
                self.stop_reason = f"Circuit breaker tripped after {self.consecutive_5xx} consecutive 5xx responses"
                raise CircuitBreakerError(self.stop_reason)

    def record_error(self, err_name: str):
        """Record a non-5xx error."""
        with self._lock:
            self.errors_by_type[err_name] = self.errors_by_type.get(err_name, 0) + 1

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_requests": self.total_requests,
                "elapsed_seconds": round(time.time() - self.start_time, 2),
                "requests_by_type": dict(self.requests_by_type),
                "errors_by_type": dict(self.errors_by_type),
                "consecutive_5xx": self.consecutive_5xx,
                "stop_requested": self.stop_requested,
                "stop_reason": self.stop_reason
            }
