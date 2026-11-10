from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import DATABASE_URL

pool = AsyncConnectionPool(DATABASE_URL, open=False, kwargs={"row_factory": dict_row})


async def init_db() -> None:
    await pool.open(wait=True)
    schema = (Path(__file__).parent / "schema.sql").read_text()
    async with pool.connection() as conn:
        await conn.execute(schema)
