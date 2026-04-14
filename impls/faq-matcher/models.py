from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Integer, Text
from database import Base


class FAQ(Base):
    """Seeded FAQ entries — the knowledge base the LLM matches against."""
    __tablename__ = "faqs"

    id       = Column(Integer, primary_key=True)
    question = Column(Text, nullable=False)
    answer   = Column(Text, nullable=False)


class FAQQuery(Base):
    """Every user question that came through /faq/match, with the result."""
    __tablename__ = "faq_queries"

    id         = Column(Integer, primary_key=True)
    question   = Column(Text, nullable=False)
    matched    = Column(Boolean, nullable=False)
    answer     = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(tz=timezone.utc))
