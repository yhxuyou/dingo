from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime


class DatasourceCreate(BaseModel):
    name: str
    type: str
    config: dict


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict] = None


class DatasourceResponse(BaseModel):
    id: int
    name: str
    type: str
    config: dict
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DatasourceTestResult(BaseModel):
    success: bool
    message: str
    tables: Optional[list[str]] = None
    columns: Optional[dict[str, list[str]]] = None


class DataBrowseResponse(BaseModel):
    columns: list[str]
    rows: list[dict]
    total: int
    page: int
    page_size: int


class RuleCreate(BaseModel):
    name: str
    metric_type: str
    group: Optional[str] = "custom"
    description: Optional[str] = None
    config: dict


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    metric_type: Optional[str] = None
    group: Optional[str] = None
    description: Optional[str] = None
    config: Optional[dict] = None


class RuleResponse(BaseModel):
    id: int
    name: str
    metric_type: str
    group: str
    is_builtin: bool
    description: Optional[str]
    config: dict
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TaskCreate(BaseModel):
    name: str
    datasource_id: int
    rule_ids: list[int]
    field_mapping: Optional[dict] = None


class TaskResponse(BaseModel):
    id: int
    name: str
    datasource_id: int
    rule_ids: str
    field_mapping: dict
    status: str
    progress: float
    total: int
    processed: int
    score: Optional[float]
    num_good: int
    num_bad: int
    summary: Optional[dict]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    class Config:
        from_attributes = True


class ReportResponse(BaseModel):
    task_id: int
    task_name: str
    status: str
    score: Optional[float]
    num_good: int
    num_bad: int
    total: int
    type_ratio: Optional[dict]
    metrics_score: Optional[dict]
    bad_data: list[dict]
