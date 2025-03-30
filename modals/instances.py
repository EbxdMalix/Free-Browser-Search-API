from pydantic import BaseModel, HttpUrl, Field, field_validator, ValidationError
from typing import List, Optional
import re


class SearxInstanceStats(BaseModel):
    """Represents the statistics for a single SearxNG instance."""
    url: HttpUrl
    search_response_time: Optional[float] = Field(
        None, description="Response time for a default engine query (seconds)")
    google_response_time: Optional[float] = Field(
        None, description="Response time for a Google engine query (seconds)")
    initial_response_time: Optional[float] = Field(
        None, description="Initial response time of the instance (seconds)")
    uptime_percent: Optional[float] = Field(None,
                                            description="Uptime percentage")

    @field_validator('search_response_time',
                     'google_response_time',
                     'initial_response_time',
                     'uptime_percent',
                     mode='before')
    def clean_float_strings(cls, v):
        """Attempt to clean and convert potential string values to float."""
        if isinstance(v, str):
            cleaned_v = re.sub(r'[^\d.-]+', '', v).strip()
            if not cleaned_v:
                return None
            try:
                return float(cleaned_v)
            except ValueError:
                return None
        elif isinstance(v, (int, float)):
            return float(v)
        return None
