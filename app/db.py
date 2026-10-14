import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://seatuser:seatpass@localhost:5432/seatdb",
)

# open=False: we open the pool when the app starts (see main.py)
pool = ConnectionPool(
    DATABASE_URL,
    min_size=2,
    max_size=10,
    kwargs={"row_factory": dict_row},  # rows come back as Python dicts
    open=False,
)