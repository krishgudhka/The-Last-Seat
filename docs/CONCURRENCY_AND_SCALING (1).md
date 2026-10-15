# Concurrency Handling and Scaling

How **The Last Seat** guarantees that no seat is booked twice when thousands of students click at once, and how the system could grow beyond one machine.

The diagrams referred to below are in `docs/diagrams/`.

---

## 1. The problem: a race condition

With 500 seats and 10,000 students, many requests for the same seat arrive within milliseconds of each other. The naive approach is two separate steps:

```
1. SELECT: is seat 12 free?   -> yes (for both students)
2. UPDATE: book seat 12       -> both succeed  =>  DOUBLE BOOKING
```

Between step 1 and step 2 the answer can change. This is called a **race condition**. It cannot be fixed with `if` statements in Python, because two requests can both pass the `if` before either one saves.

## 2. The solution: check and claim in one atomic statement

The seat row in PostgreSQL is the single source of truth. A seat is claimed with **one** `UPDATE` that contains the check inside its `WHERE` clause (`do_reserve` in `app/main.py`):

```sql
UPDATE seats
SET status = 'HELD', held_by = :user, held_until = now() + make_interval(mins => :hold)
WHERE id = :seat
  AND (status = 'AVAILABLE'
       OR (status = 'HELD' AND held_until < now()))
RETURNING id, seat_number, held_until;
```

- **1 row returned**: the student won the seat.
- **0 rows returned**: someone else got there first, so the API answers `409 Seat is not available`.

### Why this is safe

PostgreSQL takes a **row lock** on the seat while an `UPDATE` runs. If two students update the same seat at the same instant:

1. Student A's `UPDATE` gets the lock and changes the row to `HELD`.
2. Student B's `UPDATE` **waits** for A's transaction to finish.
3. When A commits, PostgreSQL re-evaluates B's `WHERE` clause against the new row. The seat is now `HELD` with a future `held_until`, so the condition is false and B updates **0 rows**.

The decision is made by the database, not by Python. That is why it stays correct even with several server processes or several servers. A Python lock (`threading.Lock`) would only protect a single process.

See `diagrams/booking-sequence.png` for the full timeline.

## 3. Everything in one transaction

`do_reserve` runs inside one database transaction:

1. Check that the user exists.
2. Claim the seat (the atomic `UPDATE` above).
3. Mark any old, expired `PENDING` booking on that seat as `EXPIRED`.
4. Insert the new `PENDING` booking.

If any step fails, the whole transaction rolls back and nothing is half-saved.

### A second safety net inside the database

`schema.sql` also defines a partial unique index:

```sql
CREATE UNIQUE INDEX one_active_booking_per_seat
    ON bookings (seat_id)
    WHERE status IN ('PENDING', 'CONFIRMED');
```

Even if a bug in the application tried to create two active bookings for one seat, PostgreSQL would reject the second insert. This is also why step 3 above first marks the old expired `PENDING` booking as `EXPIRED`: the old booking must stop being "active" before the new one can be inserted. See `ERD.md` for the full schema.

## 4. Temporary reservations that expire

Each reservation lasts `HOLD_MINUTES` (default 5). Expiry works in two layers:

| Layer | How it works | Why it exists |
|---|---|---|
| **Lazy expiry** | The reserve `WHERE` clause treats a `HELD` seat with `held_until < now()` as free. `GET /seats` and the booking endpoints also report such seats and bookings as `AVAILABLE` / `EXPIRED`. | Correct to the second, even if the background job has not run yet. |
| **Sweeper** | A background thread (`app/sweeper.py`, started in the app's `lifespan`) periodically marks expired `PENDING` bookings as `EXPIRED` and frees their seats. | Keeps the stored data tidy and the booking history accurate. |

Seat states are shown in `diagrams/seat-states.png` and booking states in `diagrams/booking-states.png`.

## 5. Payments without holding database locks

A payment can take seconds. Holding a database lock for that long would block other students, so `do_pay` works in three phases:

| Phase | What happens | Database state |
|---|---|---|
| **1. Validate** | Lock the booking row (`SELECT ... FOR UPDATE`), check it is `PENDING` and not expired, reject if a payment is already in progress, insert a `PENDING` payment, **commit**. | Lock released immediately |
| **2. Gateway** | The fake payment gateway runs (`success`, `fail` or `timeout`). | **No transaction open** |
| **3. Apply** | Lock the booking again and apply the result. | Short transaction |

Outcomes in phase 3:

| Gateway result | Reservation still valid? | Result |
|---|---|---|
| success | yes | Seat becomes `BOOKED`, booking `CONFIRMED`, payment `SUCCESS` |
| success | no (expired and taken by someone else, or cancelled) | Booking is not confirmed; payment recorded as `FAILED` with the message "Reservation was lost while paying" |
| fail | yes | Payment `FAILED`, booking `FAILED`, seat released for others |
| timeout | yes | Payment `FAILED`, booking stays `PENDING`; the student can retry before the hold ends |

**Double clicks and repeated pay requests:** the `FOR UPDATE` lock on the booking row plus the "payment already in progress" check mean two pay requests for the same booking cannot both go through. After a successful payment the booking is `CONFIRMED`, so any further attempt gets `409`.

## 6. Idempotency keys: repeated requests are safe

Networks retry requests and students double-click. Reserve and pay accept an optional `Idempotency-Key` header (`run_idempotent` in `app/main.py`):

1. **Claim the key:** `INSERT INTO idempotency_keys ... ON CONFLICT (key) DO NOTHING`. Only one request can win this insert, even if two arrive together.
2. **Key already seen:** return the saved response with the header `Idempotent-Replay: true`. If the key was used for a different request (different seat, user or booking) the API answers `422`. If the first request has not finished yet, it answers `409 ... still in progress`.
3. **First time:** run the work and save the status code and response body for later replays. Normal failures such as `409` are saved too. If an unexpected crash happens, the key is deleted so the client can retry.

## 7. Cancellation and reallocation

`POST /bookings/{id}/cancel` locks the booking row, sets it to `CANCELLED`, and releases the seat **only if this user still holds it** (`WHERE held_by = :user`). The seat becomes `AVAILABLE` and the next student can reserve it through the normal atomic path. A student can never free a seat that has since been given to someone else.

## 8. Evidence from load testing

The test used Locust with 1000 virtual students, spawned at 100 per second, against a single API process and a PostgreSQL container on one developer laptop. Each student creates an account, tries random seats until a reserve succeeds, then pays or abandons (15% abandon).

| Measure | Result |
|---|---|
| Reserve requests | 13,620, with 0 server errors |
| Pay requests | 559, with 0 server errors |
| Bookings actually created | 616 (390 confirmed, 116 failed payments, 110 abandoned or unfinished) |
| Reserve requests correctly rejected with `409` | about 13,000 |
| Seats booked more than once | **0** |
| Bookings charged more than once | **0** |
| Confirmed bookings vs booked seats | 390 = 390 |
| After the 5-minute hold passed | 110 pending bookings became `EXPIRED` and 110 seats returned to `AVAILABLE`; confirmed bookings unchanged |

Verification queries are in `db/verify.sql`; run them after every test. Locust measures speed, and only these queries show whether the data is correct.

**Limits of this test:** it ran on one laptop with Locust, the API and the database sharing the same CPU. The account-creation step (`POST /users`) failed for about 19% of requests during the initial burst of 1000 simultaneous sign-ups, with response times up to 16 seconds. This marks the ceiling of a single API process and is what the scaling plan below addresses. It did not affect booking correctness.

## 9. Known limitations

These are deliberate simplifications, listed here so they can be discussed openly:

| Limitation | Effect | Fix in a production system |
|---|---|---|
| No authentication; the user id is sent in the request body | Anyone can act as any student | Login with tokens (JWT) and take the user from the token |
| Payments are simulated and there is no refund state | A "lost reservation" or a cancelled confirmed booking does not trigger a refund | Real gateway with webhooks, a `REFUNDED` payment status |
| Lock order differs between endpoints (reserve locks seat then booking; pay locks booking then seat) | In rare timing, two requests could wait on each other and PostgreSQL would abort one with a deadlock error. None occurred in about 15,000 test requests | Always lock the seat first, or retry the request once on a deadlock error |
| A server crash during the gateway call leaves a payment in `PENDING` | The "payment already in progress" check would block retries until the reservation expires | Treat payments older than a few seconds as timed out |
| A crash after an idempotency key is claimed but before it is completed | The key reports "still in progress" until it is cleaned up | Add a timestamp and ignore claims older than a timeout |
| No waiting queue | When sold out, students must keep retrying | See section 10 |

## 10. How the application could scale

The API keeps **no state in memory**: all state lives in PostgreSQL. That means any number of API copies can run side by side.

| Bottleneck seen or expected | Remedy |
|---|---|
| One Python process handles all requests (the sign-up burst slowed down) | Run several worker processes (`uvicorn --workers N`) and several containers behind a **reverse proxy / load balancer** such as nginx |
| Limited database connections per process | Tune the connection pool size; add **PgBouncer** in front of PostgreSQL |
| Thousands of students refreshing the seat list | Cache `GET /seats` for one or two seconds (**Redis** or in the proxy); add a read replica |
| 13,000 requests for seats that are already gone | Keep a "sold out" flag in Redis and answer immediately without touching the database |
| Slow payments tie up request threads | Move payment processing to a queue (**RabbitMQ, Kafka or NATS**) with separate workers; the API returns `202 Accepted` and the client polls `GET /bookings/{id}` |
| Several sweeper threads, one per process | Run the sweeper as a single scheduled job, or guard it with a PostgreSQL advisory lock |
| Students want a fair line when sold out | **Waiting queue:** when a seat is freed, offer it to the first waiting student as a new reservation |
| Many events, not just one concert | Partition by event; the seat-claiming `UPDATE` stays the same |

The key design point for scaling: the atomic `UPDATE` serialises only requests for the **same seat**. Requests for different seats do not block each other, so adding more API servers increases throughput without risking double booking.
