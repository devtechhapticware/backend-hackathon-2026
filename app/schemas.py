"""
Pydantic models for request/response validation.

Ensures:
  - Input data is validated before reaching business logic
  - Consistent API contracts across all endpoints

Validation layers:
  1. Pydantic (API level)
  2. Database constraints (persistence level)
"""

from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


# Agent registry
class AgentRegisterRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Unique slug for this service, e.g. 'lead_scorer'",
        examples=["lead_scorer"],
    )
    category: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Domain tag, e.g. 'sales', 'marketing', 'support'",
        examples=["sales"],
    )
    endpoint: str = Field(
        ...,
        description="Base URL of the running service. No trailing slash.",
        examples=["http://192.168.1.42:8001"],
    )

    @field_validator("name")
    @classmethod
    def name_must_be_slug(cls, v: str) -> str:
        """
        Enforce lowercase-alphanumeric-underscore names.
        """
        import re
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9_]+$", v):
            raise ValueError(
                "Agent name must contain only lowercase letters, digits, and underscores. "
                "Example: 'lead_scorer', not 'Lead Scorer'."
            )
        return v

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_valid_url(cls, v: str) -> str:
        """
        Validate endpoint is a reachable-looking URL.
        Catches the common mistake of submitting a partial path instead of a base URL.
        """
        v = v.rstrip("/")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Endpoint must start with http:// or https://. Got: {v!r}"
            )
        if not parsed.netloc:
            raise ValueError(
                f"Endpoint must include a host (e.g. http://192.168.1.42:8001). Got: {v!r}"
            )
        return v


class AgentUpdateEndpointRequest(BaseModel):
    """Used by PATCH /agents/{name}/endpoint — body instead of query param so URLs are safe."""
    endpoint: str = Field(
        ...,
        description="New base URL for the agent service",
        examples=["http://192.168.1.42:8002"],
    )

    @field_validator("endpoint")
    @classmethod
    def endpoint_must_be_valid_url(cls, v: str) -> str:
        v = v.rstrip("/")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Endpoint must start with http:// or https://. Got: {v!r}"
            )
        if not parsed.netloc:
            raise ValueError(f"Endpoint must include a host. Got: {v!r}")
        return v


class AgentResponse(BaseModel):
    id: int
    name: str
    category: str
    endpoint: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# Gateway execution
class RunRequest(BaseModel):
    """
    Input contract forwarded to agent services via /run.
    """
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Domain-specific parameters for the target agent.",
    )


class RunResponse(BaseModel):
    """
    Standardized response returned by the gateway.

    Includes:
      - agent output
      - execution status
      - run_id (for feedback linkage)
      - latency
    """
    agent_name: str
    result: dict[str, Any]
    status: str  # "success" | "error"
    run_id: str  # gateway-assigned UUID for feedback linkage
    latency_ms: float  # end-to-end wall-clock time in milliseconds


# Token management
class TokenCreateRequest(BaseModel):
    scope: str = Field(
        "run",
        description="'run' for student teams (default), 'admin' for the Tech Lead only.",
    )

    @field_validator("scope")
    @classmethod
    def scope_must_be_valid(cls, v: str) -> str:
        if v not in ("run", "admin"):
            raise ValueError("scope must be 'run' or 'admin'.")
        return v


class TokenResponse(BaseModel):
    token: str
    scope: str
    created_at: datetime

    class Config:
        from_attributes = True


# Feedback
class FeedbackRequest(BaseModel):
    """
    Feedback payload linked to a specific run.
    """
    run_id: str = Field(..., description="The run_id returned by POST /run")
    rating: int = Field(
        ...,
        ge=0,
        le=1,
        description="0 = bad result, 1 = good result",
    )
    comment: Optional[str] = Field(None, max_length=1000)


class FeedbackResponse(BaseModel):
    id: int
    agent_name: str
    run_id: str
    rating: int
    comment: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# Dashboard
class DashboardResponse(BaseModel):
    total_agents: int
    online_agents: int
    offline_agents: int
    total_runs: int
    avg_latency_ms: Optional[float] = None


# Logs
class LogEntry(BaseModel):
    """
    Representation of a single execution log entry.
    """
    id: int
    agent_name: str
    input: dict
    output: dict
    latency_ms: Optional[float]
    created_at: datetime

    class Config:
        from_attributes = True
