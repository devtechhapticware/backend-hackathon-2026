"""
models.py
=========
SQLAlchemy ORM models for the five gateway-owned tables.

These map 1-to-1 with the tables defined in the assessment document (Section 11).
The Tech Lead owns these tables. Student-side tables (leads, tickets, etc.)
are managed by the student teams and must NOT be touched here.

Table summary:
  agents         — one row per registered student service
  agent_logs     — one row per /run invocation (input, output, latency)
  tokens         — bearer tokens used to authenticate gateway callers
  agent_feedback — thumbs-up/down ratings tied to a specific run_id
  health_checks  — timestamped health-ping results for every agent
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class Agent(Base):
    """
    One row per registered student microservice.

    Fields:
      name      — unique human-readable slug agreed with the team (e.g. "lead_scorer")
      category  — domain grouping (e.g. "sales", "marketing", "support")
      endpoint  — reachable base URL of the service (no trailing slash)
      status    — "online" or "offline"; kept current by the health monitor background task
      created_at — when the team registered
    """
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    category = Column(String(100), nullable=False)
    endpoint = Column(String(500), nullable=False)
    status = Column(String(50), nullable=False, default="online")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AgentLog(Base):
    """
    Immutable audit record written for every /run call.

    Fields:
      agent_name  — which agent was invoked (denormalized for query speed)
      input       — the exact {"payload": {...}} sent to the student service
      output      — the exact response received (or error dict on failure)
      latency_ms  — wall-clock time from request sent to response received
      created_at  — UTC timestamp of the call

    JSONB is used for input/output so Postgres can index and query inside
    the JSON if needed later.
    """
    __tablename__ = "agent_logs"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String(255), nullable=False, index=True)
    input = Column(JSONB, nullable=False)
    output = Column(JSONB, nullable=False)
    latency_ms = Column(Numeric(10, 2))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Token(Base):
    """
    Bearer tokens issued to gateway callers.

    Fields:
      token      — the raw token string (cryptographically random, URL-safe)
      scope      — "run" for student teams, "admin" for the Tech Lead
      created_at — when it was issued

    The scope field gates access to admin-only endpoints (token management,
    agent deregistration). Student teams always get scope="run".
    """
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True)
    token = Column(String(500), unique=True, nullable=False, index=True)
    scope = Column(String(100), nullable=False, default="run")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AgentFeedback(Base):
    """
    Quality signal per individual run, submitted by callers after seeing results.

    Fields:
      agent_name — which agent produced the result
      run_id     — the UUID returned by /run, ties feedback to a specific call
      rating     — 0 (bad) or 1 (good). DB-level CHECK constraint enforces this.
      comment    — optional free-text note
      created_at — when the feedback was submitted

    The CHECK constraint on rating is enforced at the database level, not just
    Pydantic, so malformed direct DB inserts are also rejected.
    """
    __tablename__ = "agent_feedback"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String(255), nullable=False, index=True)
    run_id = Column(String(255), nullable=False, index=True)
    rating = Column(Integer, nullable=False)
    comment = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("rating IN (0, 1)", name="ck_agent_feedback_rating_binary"),
    )


class HealthCheck(Base):
    """
    Timestamped record of each health-ping result, written by the background monitor.

    Fields:
      agent_name — which agent was pinged
      status     — "online" or "offline" based on whether /health returned 200
      checked_at — exact UTC time of the ping

    This table gives you a full history of when any service went offline or came
    back up, which is useful for debugging during the hackathon.
    """
    __tablename__ = "health_checks"

    id = Column(Integer, primary_key=True)
    agent_name = Column(String(255), nullable=False, index=True)
    status = Column(String(50), nullable=False)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
