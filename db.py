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

    async def search_dishes(self, query: str, limit: int = 10) -> list[str]:
        q = " ".join(query.strip().split())
        if len(q) < 2:
            return []

    async def delete_feedback(self, fid: int) -> None:
        await self.pool.execute("DELETE FROM feedback WHERE id=$1", fid)

        parts = [p for p in q.split(" ") if p]
        # Собираем WHERE: name ILIKE $1 AND name ILIKE $2 ...
        conds = " AND ".join([f"name ILIKE ${i+1}" for i in range(len(parts))])
        params = [f"%{p}%" for p in parts] + [limit]

        sql = f"""
            SELECT name
            FROM dishes
            WHERE {conds}
            ORDER BY name
            LIMIT ${len(parts)+1}
        """

        rows = await self.pool.fetch(sql, *params)
        return [r["name"] for r in rows]

    async def upsert_subscriber(self, chat_id: int, chat_type: str = "private") -> None:
        await self.pool.execute(
            """
            INSERT INTO subscribers(chat_id, chat_type)
            VALUES($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET chat_type=EXCLUDED.chat_type
            """,
            chat_id, chat_type
        )

    async def remove_subscriber(self, chat_id: int) -> None:
        await self.pool.execute("DELETE FROM subscribers WHERE chat_id=$1", chat_id)

    async def list_subscribers(self) -> list[int]:
        rows = await self.pool.fetch("SELECT chat_id FROM subscribers")
        return [int(r["chat_id"]) for r in rows]

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

    async def set_group_message_refs(self, fid: int, chat_id: int, message_id: int):
        await self.pool.execute(
            "UPDATE feedback SET group_chat_id=$2, group_message_id=$3 WHERE id=$1",
            fid, chat_id, message_id
        )

    async def set_group_message_refs(self, fid: int, chat_id: int, message_id: int) -> None:
        await self.pool.execute(
            "UPDATE feedback SET group_chat_id=$2, group_message_id=$3 WHERE id=$1",
            fid, chat_id, message_id
        )

