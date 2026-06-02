import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATABASE_DIR, exist_ok=True)
DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(DATABASE_DIR, 'dataquality.db')}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        result = await conn.run_sync(
            lambda sync_conn: sync_conn.execute(
                __import__("sqlalchemy").text("PRAGMA table_info(tasks)")
            )
        )
        existing_columns = {row[1] for row in result}
        if "rule_configs" not in existing_columns:
            await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE tasks ADD COLUMN rule_configs JSON"
                    )
                )
            )
        if "table_name" not in existing_columns:
            await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE tasks ADD COLUMN table_name VARCHAR(200)"
                    )
                )
            )
        if "sampling" not in existing_columns:
            await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE tasks ADD COLUMN sampling JSON"
                    )
                )
            )


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
