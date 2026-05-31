import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.database import init_db
from backend.routes_datasource import router as datasource_router
from backend.routes_rule import router as rule_router
from backend.routes_task import router as task_router

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="数据质量检测平台",
    description="基于 dingo 引擎的数据质量检测平台 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(datasource_router)
app.include_router(rule_router)
app.include_router(task_router)


@app.get("/api/dashboard")
async def dashboard():
    from backend.engine import get_builtin_rules, get_builtin_rules_grouped
    from backend.models import Datasource, Task, Rule
    from backend.database import async_session

    async with async_session() as session:
        from sqlalchemy import select, func
        ds_count = (await session.execute(select(func.count(Datasource.id)))).scalar()
        task_count = (await session.execute(select(func.count(Task.id)))).scalar()
        rule_count = (await session.execute(select(func.count(Rule.id)))).scalar()

    builtin_rules = get_builtin_rules()
    dimensions = get_builtin_rules_grouped()

    return {
        "datasource_count": ds_count,
        "task_count": task_count,
        "custom_rule_count": rule_count,
        "builtin_rule_count": len(builtin_rules),
        "dimension_count": len(dimensions),
        "dimensions": [
            {"name": k, "count": len(v)}
            for k, v in sorted(dimensions.items())
        ],
    }


if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
