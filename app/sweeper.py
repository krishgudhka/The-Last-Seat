import threading

from app.db import pool

INTERVAL_SECONDS = 5


def sweep_once() -> tuple[int, int]:
    """Clean up expired holds. Returns (bookings expired, seats released)."""
    with pool.connection() as conn:
        expired = conn.execute(
            """
            UPDATE bookings SET status = 'EXPIRED'
            WHERE status = 'PENDING' AND expires_at < now()
            """
        ).rowcount

        released = conn.execute(
            """
            UPDATE seats
            SET status = 'AVAILABLE', held_by = NULL, held_until = NULL
            WHERE status = 'HELD' AND held_until < now()
            """
        ).rowcount
    return expired, released


def run_sweeper(stop: threading.Event):
    while not stop.is_set():
        try:
            expired, released = sweep_once()
            if expired or released:
                print(f"[sweeper] expired {expired} bookings, released {released} seats")
        except Exception as e:  # never let the loop die
            print(f"[sweeper] error: {e}")
        stop.wait(INTERVAL_SECONDS)  # sleeps, but wakes up instantly on shutdown