from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, Numeric, String, Text
from database import Base


class Expense(Base):
    __tablename__ = "expenses"

    id          = Column(Integer, primary_key=True)
    employee_id = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    amount      = Column(Numeric(12, 2), nullable=False)
    category    = Column(String(100), nullable=False)
    created_at  = Column(DateTime, nullable=False, default=lambda: datetime.now(tz=timezone.utc))


class Invoice(Base):
    __tablename__ = "invoices"

    id         = Column(Integer, primary_key=True)
    vendor     = Column(String(255), nullable=False)
    amount     = Column(Numeric(12, 2), nullable=False)
    due_date   = Column(String(20))
    raw_text   = Column(Text, nullable=False)
    line_items = Column(Text, nullable=False, default="[]")  # JSON string
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(tz=timezone.utc))


class FinanceQA(Base):
    __tablename__ = "finance_qa"

    id         = Column(Integer, primary_key=True)
    question   = Column(Text, nullable=False)
    answer     = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(tz=timezone.utc))
