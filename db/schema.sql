CREATE TABLE users (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);

CREATE TABLE seats (
    id          SERIAL PRIMARY KEY,
    seat_number TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'AVAILABLE'
                CHECK (status IN ('AVAILABLE', 'HELD', 'BOOKED')),
    held_by     INT REFERENCES users(id),
    held_until  TIMESTAMPTZ
);

CREATE TABLE bookings (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id),
    seat_id    INT NOT NULL REFERENCES seats(id),
    status     TEXT NOT NULL DEFAULT 'PENDING'
               CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'EXPIRED', 'FAILED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- Safety net: a seat can have only ONE active booking at a time
CREATE UNIQUE INDEX one_active_booking_per_seat
    ON bookings (seat_id)
    WHERE status IN ('PENDING', 'CONFIRMED');

CREATE TABLE payments (
    id              SERIAL PRIMARY KEY,
    booking_id      INT NOT NULL REFERENCES bookings(id),
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED')),
    idempotency_key TEXT UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
    key         TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    status_code INT,
    response    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed 500 seats: S001 ... S500
INSERT INTO seats (seat_number)
SELECT 'S' || lpad(n::text, 3, '0')
FROM generate_series(1, 500) AS n;