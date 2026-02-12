import os
import asyncio
from dotenv import load_dotenv
from db import DB

load_dotenv(dotenv_path=".env")

async def main():
    dsn = os.environ["postgresql://postgres:iRHmmepUJRBAYigcRGgYDVZgUqMsazIN@postgres.railway.internal:5432/railway"]
    db = DB(dsn)
    await db.connect()

    added = 0
    with open("dishes.txt", "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if not name:
                continue
            await db.upsert_dish(name)
            added += 1

    await db.close()
    print(f"Imported {added} dishes")

asyncio.run(main())
