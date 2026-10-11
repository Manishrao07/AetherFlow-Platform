"""Shared schema pieces: pagination.

What: ``PageParams`` (limit/offset query parameters) and the generic ``Page[T]`` response.
Why: every list endpoint paginates the same way, with a hard cap so a client cannot ask the
database for a million rows in one request.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

MAX_PAGE_SIZE = 100


class PageParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size (max 100)")] = 20,
    offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
) -> PageParams:
    """FastAPI dependency turning query params into a validated ``PageParams``."""
    return PageParams(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
