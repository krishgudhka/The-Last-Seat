-- seats by status
SELECT status, count(*) FROM seats GROUP BY status ORDER BY status;

-- bookings by status
SELECT status, count(*) FROM bookings GROUP BY status ORDER BY status;

-- must return 0 rows: seat with more than one active booking
SELECT seat_id, count(*) FROM bookings
WHERE status IN ('PENDING', 'CONFIRMED') GROUP BY seat_id HAVING count(*) > 1;

-- must return 0 rows: seat with more than one CONFIRMED booking
SELECT seat_id, count(*) FROM bookings
WHERE status = 'CONFIRMED' GROUP BY seat_id HAVING count(*) > 1;

-- must return 0 rows: booking charged more than once
SELECT booking_id, count(*) FROM payments
WHERE status = 'SUCCESS' GROUP BY booking_id HAVING count(*) > 1;

-- these two numbers must be equal
SELECT (SELECT count(*) FROM bookings WHERE status = 'CONFIRMED') AS confirmed_bookings,
       (SELECT count(*) FROM seats WHERE status = 'BOOKED')       AS booked_seats;