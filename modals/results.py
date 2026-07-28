from pydantic import BaseModel
from typing import List

class SearchResultItem(BaseModel):
    title: str
    link: str
    snippet: str
    domain: str
    context: str
    score: float

class SearchMeta(BaseModel):
    cached: bool
    took_ms: int

class SearchResponse(BaseModel):
    query: str
    results: List[SearchResultItem]
    meta: SearchMeta
