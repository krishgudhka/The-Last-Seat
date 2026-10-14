from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()    # runs when the server starts
    yield
    pool.close()   # runs when the server stops


app = FastAPI(title="The Last Seat", lifespan=lifespan)


@app.get("/")
def home():
    return {"message": "Last Seat API is running"}


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