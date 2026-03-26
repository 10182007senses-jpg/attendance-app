import os
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import (
    create_engine,
    inspect,
    String,
    Integer,
    DateTime,
    Float,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    Date,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.engine import Engine


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JST = ZoneInfo("Asia/Tokyo")


def now_jst_naive() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)

# ローカルは attendance.db、Render free は /tmp に逃がす
LOCAL_DB_PATH = os.path.join(BASE_DIR, "attendance.db")
TMP_DB_PATH = "/tmp/attendance.db"

DEFAULT_DATABASE_URL = (
    f"sqlite:///{TMP_DB_PATH}"
    if os.getenv("RENDER")  # Render で勝手に入る環境変数
    else f"sqlite:///{LOCAL_DB_PATH}"
)


def get_database_url() -> str:
    """
    優先順位:
    1. 環境変数 DATABASE_URL
    2. Render free 用 sqlite (/tmp)
    3. ローカル sqlite
    """
    database_url = os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL

    # 互換対応:
    # 一部環境では postgres:// が渡ることがあるため
    # SQLAlchemy が期待する postgresql:// に正規化する
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    return database_url


engine: Optional[Engine] = None
SessionLocal = sessionmaker(autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    pin_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    required_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=160)
    max_work_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    logs: Mapped[list["AttendanceLog"]] = relationship(back_populates="user")
    workdays: Mapped[list["Workday"]] = relationship(back_populates="user")
    monthly_requirements: Mapped[list["UserMonthlyRequirement"]] = relationship(back_populates="user")
    monthly_confirmations: Mapped[list["MonthlyConfirmation"]] = relationship(back_populates="user")


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
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_workdays_user_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)  # type: ignore

    status: Mapped[str] = mapped_column(String(20), default="open")
    employee_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive, onupdate=now_jst_naive)

    user: Mapped["User"] = relationship(back_populates="workdays")


class UserMonthlyRequirement(Base):
    __tablename__ = "user_monthly_requirements"
    __table_args__ = (UniqueConstraint("user_id", "year", "month", name="uq_user_monthly_requirements_user_year_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    required_hours: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive, onupdate=now_jst_naive)

    user: Mapped["User"] = relationship(back_populates="monthly_requirements")


class MonthlyConfirmation(Base):
    __tablename__ = "monthly_confirmations"
    __table_args__ = (UniqueConstraint("user_id", "year", "month", name="uq_monthly_confirmations_user_year_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive, onupdate=now_jst_naive)

    user: Mapped["User"] = relationship(back_populates="monthly_confirmations")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_jst_naive, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    device_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    user: Mapped["User"] = relationship()


def init_engine() -> Engine:
    """
    エンジンと SessionLocal を初期化（複数回呼ばれてもOK）
    """
    global engine
    if engine is not None:
        return engine

    database_url = get_database_url()

    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
        )

    SessionLocal.configure(bind=engine)
    return engine


def init_db() -> None:
    """
    テーブル作成。engine が未初期化なら init_engine() する。
    """
    eng = init_engine()
    Base.metadata.create_all(bind=eng)
    inspector = inspect(eng)
    if inspector.has_table("workdays"):
        columns = {col["name"] for col in inspector.get_columns("workdays")}
        if "employee_note" not in columns:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE workdays ADD COLUMN employee_note VARCHAR(500)"))
    db = SessionLocal()
    try:
        db.query(User).filter(User.required_hours.is_(None)).update(
            {User.required_hours: 160},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
