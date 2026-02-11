import time

from src.db.models import CircuitState
from src.poller.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


def test_starts_closed():
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED
    assert cb.check_allowed()


def test_opens_after_threshold():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.check_allowed()


def test_half_open_after_timeout():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.01))

    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    time.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.check_allowed()


def test_closes_after_success_threshold():
    cb = CircuitBreaker(CircuitBreakerConfig(
        failure_threshold=1, recovery_timeout=0.01, success_threshold=2
    ))

    cb.record_failure()
    time.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN  # Aún necesita 1 más

    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_half_open_failure_reopens():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.01))

    cb.record_failure()
    time.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_reset():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    cb.reset()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
    assert cb.check_allowed()


def test_success_resets_failure_count_in_closed():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
    cb.record_failure()
    cb.record_failure()
    assert cb.failure_count == 2

    cb.record_success()
    assert cb.failure_count == 0
