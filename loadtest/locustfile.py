import random
import uuid

from locust import HttpUser, between, task

TOTAL_SEATS = 500
MAX_ATTEMPTS = 20        # a student gives up after this many tries
ABANDON_CHANCE = 0.15    # 15% grab a seat and never pay


class Student(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        email = f"{uuid.uuid4().hex}@college.test"
        resp = self.client.post(
            "/users", json={"name": "Student", "email": email}, name="POST /users"
        )
        self.user_id = resp.json()["id"]
        self.attempts = 0

    @task
    def book_a_seat(self):
        self.attempts += 1
        if self.attempts > MAX_ATTEMPTS:
            self.stop()                 # gave up
            return

        booking_id = self.reserve(random.randint(1, TOTAL_SEATS))
        if booking_id is None:
            return                      # seat taken: next loop tries another seat

        if random.random() < ABANDON_CHANCE:
            self.stop()                 # abandons the booking, never pays
            return

        if self.pay(booking_id):
            self.stop()                 # confirmed seat: done
        # otherwise payment failed, seat released: loop and try again

    def reserve(self, seat_id):
        with self.client.post(
            f"/seats/{seat_id}/reserve",
            json={"user_id": self.user_id},
            name="POST /seats/[id]/reserve",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                resp.success()
                return resp.json()["booking"]["id"]
            if resp.status_code == 409:
                resp.success()          # "seat taken" is the correct answer
                return None
            resp.failure(f"unexpected status {resp.status_code}")
            return None

    def pay(self, booking_id):
        for _ in range(2):              # one retry after a timeout
            with self.client.post(
                f"/bookings/{booking_id}/pay",
                json={},                # empty body: random payment outcome
                name="POST /bookings/[id]/pay",
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"unexpected status {resp.status_code}")
                    return False
                outcome = resp.json()["outcome"]
            if outcome == "success":
                return True
            if outcome == "fail":
                return False
            # timeout: loop once more
        return False