DELETE FROM payments;
DELETE FROM idempotency_keys;
DELETE FROM bookings;
UPDATE seats SET status = 'AVAILABLE', held_by = NULL, held_until = NULL;
DELETE FROM users;