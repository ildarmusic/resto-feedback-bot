import asyncpg

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS dishes (
  id SERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  id SERIAL PRIMARY KEY,
  feedback_date DATE NOT NULL,
  dish_name TEXT NOT NULL,
  guest_comment TEXT NOT NULL,
  kitchen_reply TEXT NULL,
  telegram_chat_id BIGINT NULL,
  telegram_message_id BIGINT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dishes_name ON dishes (name);
CREATE INDEX IF NOT EXISTS idx_feedback_id ON feedback (id);
"""

class DB:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(CREATE_SQL)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def search_dishes(self, prefix: str, limit: int = 10) -> list[str]:
        assert self.pool
        q = """
        SELECT name FROM dishes
        WHERE name ILIKE $1
        ORDER BY name
        LIMIT $2
        """
        rows = await self.pool.fetch(q, prefix + "%", limit)
        return [r["name"] for r in rows]

    async def upsert_dish(self, name: str):
        assert self.pool
        q = "INSERT INTO dishes(name) VALUES($1) ON CONFLICT (name) DO NOTHING"
        await self.pool.execute(q, name.strip())

    async def create_feedback(self, feedback_date, dish_name: str, guest_comment: str, kitchen_reply: str | None):
        assert self.pool
        q = """
        INSERT INTO feedback(feedback_date, dish_name, guest_comment, kitchen_reply)
        VALUES($1, $2, $3, $4)
        RETURNING id
        """
        return await self.pool.fetchval(q, feedback_date, dish_name, guest_comment, kitchen_reply)

    async def set_message_refs(self, feedback_id: int, chat_id: int, message_id: int):
        assert self.pool
        q = """
        UPDATE feedback
        SET telegram_chat_id=$2, telegram_message_id=$3
        WHERE id=$1
        """
        await self.pool.execute(q, feedback_id, chat_id, message_id)

    async def get_feedback(self, feedback_id: int):
        assert self.pool
        return await self.pool.fetchrow("SELECT * FROM feedback WHERE id=$1", feedback_id)

    async def update_kitchen_reply(self, feedback_id: int, kitchen_reply: str):
        assert self.pool
        await self.pool.execute("UPDATE feedback SET kitchen_reply=$2 WHERE id=$1", feedback_id, kitchen_reply)
