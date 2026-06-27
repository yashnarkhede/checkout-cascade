"""asyncpg access: a charge pool + a dedicated health pool so chaos can't starve /health."""
import time
import asyncpg


async def create_pools(dsn, pool_max, health_max, statement_timeout_ms):
    server_settings = {"statement_timeout": str(int(statement_timeout_ms))}
    charge = await asyncpg.create_pool(
        dsn, min_size=2, max_size=int(pool_max), server_settings=server_settings,
    )
    health = await asyncpg.create_pool(
        dsn, min_size=1, max_size=int(health_max),
    )
    return charge, health


async def run_migration(charge_pool):
    await charge_pool.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


async def run_charge(charge_pool, user_id, amount, db_slowdown, acquire_timeout):
    started = time.monotonic()
    async with charge_pool.acquire(timeout=acquire_timeout) as conn:
        if db_slowdown and db_slowdown > 0:
            await conn.execute("SELECT pg_sleep($1)", float(db_slowdown))
        tx_id = await conn.fetchval(
            "INSERT INTO transactions (user_id, amount, status) "
            "VALUES ($1, $2, 'success') RETURNING id",
            user_id, amount,
        )
        await conn.fetchrow("SELECT id FROM transactions WHERE id = $1", tx_id)  # verifying read
    return tx_id, (time.monotonic() - started) * 1000.0


async def health_ping(health_pool, timeout):
    async with health_pool.acquire(timeout=timeout) as conn:
        return (await conn.fetchval("SELECT 1")) == 1
