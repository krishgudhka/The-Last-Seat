import os
import random
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.db import pool
from app.sweeper import run_sweeper

HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "5"))
PAYMENT_MAX_DELAY = float(os.getenv("PAYMENT_MAX_DELAY", "3"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    stop = threading.Event()
    worker = threading.Thread(target=run_sweeper, args=(stop,), daemon=True)
    worker.start()          # sweeper begins in the background
    yield
    stop.set()              # tell the sweeper to stop
    worker.join(timeout=5)
    pool.close()

app = FastAPI(title="The Last Seat", lifespan=lifespan)


# ---------- request bodies ----------
class UserIn(BaseModel):
    name: str
    email: str


class ReserveIn(BaseModel):
    user_id: int

class PayIn(BaseModel):
    # leave empty for a random outcome; set it to force one while testing
    outcome: Literal["success", "fail", "timeout"] | None = None

# ---------- endpoints ----------
@app.get("/")
def home():
    return {"message": "Last Seat API is running"}


@app.post("/users", status_code=201)
def create_user(user: UserIn):
    # If the email already exists, just return that user (safe to repeat)
    query = """
        INSERT INTO users (name, email) VALUES (%s, %s)
        ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, name, email
    """
    with pool.connection() as conn:
        return conn.execute(query, (user.name, user.email)).fetchone()


@app.get("/seats")
def list_seats(available_only: bool = False):
    query = """
        SELECT * FROM (
            SELECT id,
                   seat_number,
                   CASE WHEN status = 'HELD' AND held_until < now()
                        THEN 'AVAILABLE'
                        ELSE status
                   END AS status
            FROM seats
        ) AS s
        WHERE (%s = false OR s.status = 'AVAILABLE')
        ORDER BY id
    """
    with pool.connection() as conn:
        rows = conn.execute(query, (available_only,)).fetchall()
    return {"count": len(rows), "seats": rows}


@app.post("/seats/{seat_id}/reserve", status_code=201)
def reserve_seat(seat_id: int, body: ReserveIn):
    with pool.connection() as conn:  # one transaction: commits at the end
        # 0. does the user exist?
        user = conn.execute(
            "SELECT id FROM users WHERE id = %s", (body.user_id,)
        ).fetchone()
        if user is None:
            raise HTTPException(404, "User not found")

        # 1. THE atomic step: check + claim in a single UPDATE
        won = conn.execute(
            """
            UPDATE seats
            SET status = 'HELD',
                held_by = %s,
                held_until = now() + make_interval(mins => %s)
            WHERE id = %s
              AND (status = 'AVAILABLE'
                   OR (status = 'HELD' AND held_until < now()))
            RETURNING id, seat_number, held_until
            """,
            (body.user_id, HOLD_MINUTES, seat_id),
        ).fetchone()

        if won is None:
            exists = conn.execute(
                "SELECT 1 FROM seats WHERE id = %s", (seat_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(404, "Seat not found")
            raise HTTPException(409, "Seat is not available")

        # 2. clear out an old, expired PENDING booking on this seat (if any)
        conn.execute(
            """
            UPDATE bookings SET status = 'EXPIRED'
            WHERE seat_id = %s AND status = 'PENDING' AND expires_at < now()
            """,
            (seat_id,),
        )

        # 3. create the new booking
        booking = conn.execute(
            """
            INSERT INTO bookings (user_id, seat_id, status, expires_at)
            VALUES (%s, %s, 'PENDING', %s)
            RETURNING id, user_id, seat_id, status, expires_at
            """,
            (body.user_id, seat_id, won["held_until"]),
        ).fetchone()

    return {"booking": booking, "seat_number": won["seat_number"]}

# ---------- helpers ----------
def release_seat(conn, seat_id: int, user_id: int):
    """Give the seat back, but only if this user still holds it."""
    conn.execute(
        """
        UPDATE seats
        SET status = 'AVAILABLE', held_by = NULL, held_until = NULL
        WHERE id = %s AND held_by = %s
        """,
        (seat_id, user_id),
    )


def simulate_payment(forced: str | None) -> str:
    """Fake payment gateway: 70% success, 20% declined, 10% timeout."""
    outcome = forced or random.choices(
        ["success", "fail", "timeout"], weights=[70, 20, 10]
    )[0]
    if outcome == "timeout":
        time.sleep(PAYMENT_MAX_DELAY + 2)          # gateway is slow
    else:
        time.sleep(random.uniform(0, PAYMENT_MAX_DELAY))
    return outcome


# ---------- booking endpoints ----------
@app.get("/bookings/{booking_id}")
def get_booking(booking_id: int):
    query = """
        SELECT b.id, b.user_id, b.seat_id, s.seat_number,
               CASE WHEN b.status = 'PENDING' AND b.expires_at < now()
                    THEN 'EXPIRED' ELSE b.status END AS status,
               b.created_at, b.expires_at
        FROM bookings b
        JOIN seats s ON s.id = b.seat_id
        WHERE b.id = %s
    """
    with pool.connection() as conn:
        row = conn.execute(query, (booking_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Booking not found")
    return row


@app.post("/bookings/{booking_id}/pay")
def pay_booking(booking_id: int, body: PayIn = PayIn()):
    # ---- phase 1: validate and record a PENDING payment ----
    with pool.connection() as conn:
        booking = conn.execute(
            """
            SELECT id, user_id, seat_id, status, expires_at < now() AS expired
            FROM bookings WHERE id = %s FOR UPDATE
            """,
            (booking_id,),
        ).fetchone()
        if booking is None:
            raise HTTPException(404, "Booking not found")
        if booking["status"] != "PENDING":
            raise HTTPException(409, f"Booking is {booking['status']}")
        if booking["expired"]:
            raise HTTPException(409, "Reservation expired")

        in_flight = conn.execute(
            "SELECT 1 FROM payments WHERE booking_id = %s AND status = 'PENDING'",
            (booking_id,),
        ).fetchone()
        if in_flight:
            raise HTTPException(409, "A payment is already in progress")

        payment_id = conn.execute(
            "INSERT INTO payments (booking_id) VALUES (%s) RETURNING id",
            (booking_id,),
        ).fetchone()["id"]
    # the transaction is committed here and the lock is released

    # ---- phase 2: the fake gateway (no transaction open) ----
    outcome = simulate_payment(body.outcome)

    # ---- phase 3: apply the result ----
    with pool.connection() as conn:
        booking = conn.execute(
            "SELECT user_id, seat_id, status FROM bookings WHERE id = %s FOR UPDATE",
            (booking_id,),
        ).fetchone()
        still_pending = booking["status"] == "PENDING"

        if outcome == "success" and still_pending:
            seat = conn.execute(
                """
                UPDATE seats SET status = 'BOOKED', held_until = NULL
                WHERE id = %s AND held_by = %s AND status = 'HELD'
                RETURNING id
                """,
                (booking["seat_id"], booking["user_id"]),
            ).fetchone()
            if seat:
                conn.execute(
                    "UPDATE bookings SET status = 'CONFIRMED' WHERE id = %s",
                    (booking_id,),
                )
                conn.execute(
                    "UPDATE payments SET status = 'SUCCESS' WHERE id = %s",
                    (payment_id,),
                )
                return {"payment_id": payment_id, "outcome": "success",
                        "booking_status": "CONFIRMED", "message": "Seat booked!"}

        # everything below means the payment did not complete
        conn.execute(
            "UPDATE payments SET status = 'FAILED' WHERE id = %s", (payment_id,)
        )

        if outcome == "fail" and still_pending:
            release_seat(conn, booking["seat_id"], booking["user_id"])
            conn.execute(
                "UPDATE bookings SET status = 'FAILED' WHERE id = %s", (booking_id,)
            )
            return {"payment_id": payment_id, "outcome": "fail",
                    "booking_status": "FAILED",
                    "message": "Payment declined. Seat released."}

        if outcome == "timeout" and still_pending:
            return {"payment_id": payment_id, "outcome": "timeout",
                    "booking_status": "PENDING",
                    "message": "Payment timed out. Retry before the hold expires."}

        return {"payment_id": payment_id, "outcome": outcome,
                "booking_status": booking["status"],
                "message": "Reservation was lost while paying. No charge made."}


@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int):
    with pool.connection() as conn:
        booking = conn.execute(
            "SELECT user_id, seat_id, status FROM bookings WHERE id = %s FOR UPDATE",
            (booking_id,),
        ).fetchone()
        if booking is None:
            raise HTTPException(404, "Booking not found")
        if booking["status"] not in ("PENDING", "CONFIRMED"):
            raise HTTPException(409, f"Booking is already {booking['status']}")

        conn.execute(
            "UPDATE bookings SET status = 'CANCELLED' WHERE id = %s", (booking_id,)
        )
        release_seat(conn, booking["seat_id"], booking["user_id"])
    return {"booking_id": booking_id, "status": "CANCELLED",
            "message": "Seat is available again"}