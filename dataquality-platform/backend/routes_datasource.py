import json
import os
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, UploadFile, File, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.models import Datasource, DatasourceType, Task, TaskStatus, Rule
from backend.schemas import (
    DatasourceCreate, DatasourceUpdate, DatasourceResponse, DatasourceTestResult,
    DataBrowseResponse,
)
from backend.engine import UPLOAD_DIR

router = APIRouter(prefix="/api/datasources", tags=["datasources"])


def _to_response(ds: Datasource) -> DatasourceResponse:
    return DatasourceResponse(
        id=ds.id,
        name=ds.name,
        type=ds.type.value if isinstance(ds.type, DatasourceType) else ds.type,
        config=ds.config,
        created_at=ds.created_at,
        updated_at=ds.updated_at,
    )


@router.get("", response_model=list[DatasourceResponse])
async def list_datasources(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Datasource).order_by(Datasource.id.desc()))
    return [_to_response(ds) for ds in result.scalars().all()]


@router.post("", response_model=DatasourceResponse)
async def create_datasource(
    body: DatasourceCreate,
    session: AsyncSession = Depends(get_session),
):
    ds = Datasource(
        name=body.name,
        type=body.type,
        config=body.config,
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return _to_response(ds)


@router.get("/{ds_id}", response_model=DatasourceResponse)
async def get_datasource(ds_id: int, session: AsyncSession = Depends(get_session)):
    ds = await session.get(Datasource, ds_id)
    if not ds:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Datasource not found")
    return _to_response(ds)


@router.put("/{ds_id}", response_model=DatasourceResponse)
async def update_datasource(
    ds_id: int,
    body: DatasourceUpdate,
    session: AsyncSession = Depends(get_session),
):
    ds = await session.get(Datasource, ds_id)
    if not ds:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Datasource not found")
    if body.name is not None:
        ds.name = body.name
    if body.config is not None:
        ds.config = body.config
    await session.commit()
    await session.refresh(ds)
    return _to_response(ds)


@router.delete("/{ds_id}")
async def delete_datasource(ds_id: int, session: AsyncSession = Depends(get_session)):
    ds = await session.get(Datasource, ds_id)
    if not ds:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Datasource not found")
    await session.delete(ds)
    await session.commit()
    return {"ok": True}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1].lower()
    allowed = {".json", ".jsonl", ".csv", ".xlsx", ".xls", ".parquet", ".txt"}
    if ext not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    file_id = str(abs(hash(file.filename)))[:8]
    save_dir = os.path.join(UPLOAD_DIR, file_id)
    os.makedirs(save_dir, exist_ok=True)
    file_path = os.path.join(save_dir, file.filename)

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    fmt_map = {
        ".json": "json", ".jsonl": "jsonl", ".txt": "plaintext",
        ".csv": "csv", ".xlsx": "excel", ".xls": "excel",
        ".parquet": "parquet",
    }

    return {
        "file_path": file_path,
        "filename": file.filename,
        "format": fmt_map.get(ext, "jsonl"),
        "size": len(content),
    }


@router.post("/{ds_id}/test", response_model=DatasourceTestResult)
async def test_datasource(ds_id: int, session: AsyncSession = Depends(get_session)):
    ds = await session.get(Datasource, ds_id)
    if not ds:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Datasource not found")

    ds_type = ds.type.value if isinstance(ds.type, DatasourceType) else ds.type
    config = ds.config

    if ds_type == "local_file":
        file_path = config.get("file_path", "")
        if not os.path.exists(file_path):
            return DatasourceTestResult(success=False, message=f"File not found: {file_path}")
        return DatasourceTestResult(success=True, message="File accessible")

    try:
        from sqlalchemy import create_engine, text, inspect
        dialect_map = {
            "mysql": "mysql+pymysql",
            "postgresql": "postgresql+psycopg2",
            "oracle": "oracle+cx_oracle",
            "sqlserver": "mssql+pyodbc",
            "sqlite": "sqlite",
        }
        dialect = dialect_map.get(ds_type, ds_type)
        password_part = f":{config.get('password', '')}" if config.get("password") else ""
        port_part = f":{config.get('port', '')}" if config.get("port") else ""

        if ds_type == "sqlite":
            url = f"sqlite:///{config.get('database', '')}"
        else:
            url = (
                f"{dialect}://"
                f"{config.get('username', '')}{password_part}@"
                f"{config.get('host', '')}{port_part}/{config.get('database', '')}"
            )
            if config.get("connect_args"):
                args = config["connect_args"]
                if not args.startswith("?"):
                    args = f"?{args}"
                url = f"{url}{args}"

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        inspector = inspect(engine)
        tables = inspector.get_table_names()
        columns = {}
        for table in tables[:20]:
            cols = inspector.get_columns(table)
            columns[table] = [c["name"] for c in cols]

        engine.dispose()
        return DatasourceTestResult(
            success=True,
            message="Connection successful",
            tables=tables,
            columns=columns,
        )
    except Exception as e:
        return DatasourceTestResult(success=False, message=str(e))


@router.get("/{ds_id}/browse", response_model=DataBrowseResponse)
async def browse_data(
    ds_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    table: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    ds = await session.get(Datasource, ds_id)
    if not ds:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Datasource not found")

    ds_type = ds.type.value if isinstance(ds.type, DatasourceType) else ds.type
    config = ds.config

    if ds_type == "local_file":
        return await _browse_file(config, page, page_size)

    return await _browse_database(ds_type, config, page, page_size, table)


async def _browse_file(config: dict, page: int, page_size: int) -> DataBrowseResponse:
    import json as json_mod

    file_path = config.get("file_path", "")
    fmt = config.get("format", "jsonl")

    if not os.path.exists(file_path):
        return DataBrowseResponse(columns=[], rows=[], total=0, page=page, page_size=page_size)

    all_rows = []

    if fmt in ("jsonl", "json"):
        with open(file_path, "r", encoding="utf-8") as f:
            if fmt == "json":
                data = json_mod.load(f)
                if isinstance(data, list):
                    all_rows = data
                else:
                    all_rows = [data]
            else:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_rows.append(json_mod.loads(line))
                        except json_mod.JSONDecodeError:
                            continue
    elif fmt == "csv":
        import csv
        with open(file_path, "r", encoding=config.get("encoding", "utf-8"), newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                all_rows.append(dict(row))
    elif fmt == "plaintext":
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_rows.append({"content": line})
    elif fmt in ("excel", "parquet"):
        pass

    columns = list(all_rows[0].keys()) if all_rows else []
    total = len(all_rows)
    start = (page - 1) * page_size
    end = start + page_size
    rows = all_rows[start:end]

    return DataBrowseResponse(columns=columns, rows=rows, total=total, page=page, page_size=page_size)


async def _browse_database(
    ds_type: str, config: dict, page: int, page_size: int, table: Optional[str]
) -> DataBrowseResponse:
    if not table:
        return DataBrowseResponse(columns=[], rows=[], total=0, page=page, page_size=page_size)

    try:
        from sqlalchemy import create_engine, text
        dialect_map = {
            "mysql": "mysql+pymysql",
            "postgresql": "postgresql+psycopg2",
            "oracle": "oracle+cx_oracle",
            "sqlserver": "mssql+pyodbc",
            "sqlite": "sqlite",
        }
        dialect = dialect_map.get(ds_type, ds_type)
        password_part = f":{config.get('password', '')}" if config.get("password") else ""
        port_part = f":{config.get('port', '')}" if config.get("port") else ""

        if ds_type == "sqlite":
            url = f"sqlite:///{config.get('database', '')}"
        else:
            url = (
                f"{dialect}://"
                f"{config.get('username', '')}{password_part}@"
                f"{config.get('host', '')}{port_part}/{config.get('database', '')}"
            )

        engine = create_engine(url)
        offset = (page - 1) * page_size

        with engine.connect() as conn:
            count_result = conn.execute(text(f"SELECT COUNT(*) FROM \"{table}\""))
            total = count_result.scalar()

            result = conn.execute(
                text(f'SELECT * FROM "{table}" LIMIT :limit OFFSET :offset'),
                {"limit": page_size, "offset": offset},
            )
            columns = list(result.keys())
            rows = [dict(row._mapping) for row in result]

        engine.dispose()

        for row in rows:
            for k, v in row.items():
                if not isinstance(v, (str, int, float, bool, type(None))):
                    row[k] = str(v)

        return DataBrowseResponse(columns=columns, rows=rows, total=total, page=page, page_size=page_size)
    except Exception as e:
        return DataBrowseResponse(columns=[], rows=[], total=0, page=page, page_size=page_size)
