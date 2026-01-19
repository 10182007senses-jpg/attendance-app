import os
from datetime import datetime, date
from typing import Optional
from sqlalchemy import (
  create_engine,
  String,
  Integer,
  DateTime,
  Float,
  Boolean,
  ForeignKey,
  UniqueConstraint,
  Date,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = "sqlite:////tmp/attendance.db"



engine = create_engine(
  DATABASE_URL, connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
  pass

class User(Base):
  __tablename__ = "users"

  id:Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
  name:Mapped[str] = mapped_column(String(100), unique=True, index=True)
  pin_hash: Mapped[str] = mapped_column(String(255))
  role: Mapped[str] = mapped_column(String(20), default="user")
  is_active: Mapped[bool] = mapped_column(Boolean, default=True)

  logs: Mapped[list["AttendanceLog"]] = relationship(back_populates="user")
  workdays: Mapped[list["Workday"]] = relationship(back_populates="user")

class AttendanceLog(Base):
  __tablename__ = "attendance_logs"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
  user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

  action: Mapped[str] = mapped_column(String(20), index=True)
  ts: Mapped[datetime] = mapped_column(DateTime, index=True)

  lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
  lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
  source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

  user: Mapped["User"] = relationship(back_populates="logs")

class Workday(Base):
  __tablename__ = "workdays"
  __table_args__ = (
    UniqueConstraint("user_id", "date", name="uq_workdays_user_date"),
  )

  id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
  user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
  date: Mapped[date] = mapped_column(Date, index=True) # type: ignore

  status: Mapped[str] = mapped_column(String(20), default="open")
  created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
  updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

  user: Mapped["User"] = relationship(back_populates="workdays")

class Session(Base):
  __tablename__ = "sessions"

  id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
  user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

  created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
  expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

  last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
  revoked: Mapped[bool] = mapped_column(Boolean, default=False)

  device_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

  user: Mapped["User"] = relationship()

def init_db():
  Base.metadata.create_all(bind=engine)
