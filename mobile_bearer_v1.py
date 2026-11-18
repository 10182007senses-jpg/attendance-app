"""
Mobile Bearer MVP router for the existing FastAPI attendance backend.

Place this file alongside the existing backend's `db.py` and `security.py`,
then include the router from `main.py`:

    from mobile_bearer_v1 import mobile_v1_router
    app.include_router(mobile_v1_router)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db import (
    Session as DbSession,
    SessionLocal,
    AttendanceLog,
    MonthlyConfirmation,
    User,
    UserMonthlyRequirement,
    Workday,
    now_jst_naive,
)
from security import (
    LOGIN_BLOCKED_MESSAGE,
    PIN_MAX_LENGTH,
    clear_login_failures,
    hash_token as _hash_token,
    login_blocked,
    new_session_token,
    record_login_failure,
    verify_pin_or_dummy,
)

ACTION_IN = "入室"
ACTION_OUT = "退室"
ACTIONS = (ACTION_IN, ACTION_OUT)

STATE_NOT_IN = "not_in"
STATE_IN_ROOM = "in_room"
STATE_UNKNOWN = "unknown"

BREAK_RULE_STANDARD = "standard"
BREAK_RULE_ALWAYS_1H = "always_1h"
DEFAULT_REQUIRED_HOURS = 160
ROUND_MINUTES = 30

ACCESS_TOKEN_TTL = timedelta(hours=8)
REFRESH_TOKEN_TTL = timedelta(days=30)
# アクセストークンは最後の利用から2時間で失効。リフレッシュトークンは TTL のみで管理。
ACCESS_IDLE_TIMEOUT = timedelta(hours=2)
REFRESH_IDLE_TIMEOUT: timedelta | None = None

MOBILE_ACCESS_LABEL = "mobile_access"
MOBILE_REFRESH_LABEL = "mobile_refresh"

mobile_v1_router = APIRouter(prefix="/v1", tags=["mobile-v1"])
bearer_scheme = HTTPBearer(auto_error=False)


class MobileLoginRequest(BaseModel):
    login_id: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=PIN_MAX_LENGTH)
    device_name: str | None = Field(default=None, max_length=80)


class MobileRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class MobileLogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=256)


class MobileLocationPayload(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=100_000)
    acquired_at: str | None = Field(default=None, max_length=64)
    location_error: str | None = Field(default=None, max_length=200)


class MobileClockRequest(BaseModel):
    action: Literal["clock_in", "clock_out"]
    location: MobileLocationPayload | None = None


class MobileTodayNoteRequest(BaseModel):
    note: str = Field(default="", max_length=500)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sec_to_hm(total_seconds: int) -> str:
    total_minutes = max(total_seconds, 0) // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}時間{minutes}分"


def ceil_time(dt: datetime, minutes: int = ROUND_MINUTES) -> datetime:
    dt0 = dt.replace(second=0, microsecond=0)
    remainder = dt0.minute % minutes
    if remainder == 0:
        return dt0
    return dt0 + timedelta(minutes=minutes - remainder)


def floor_time(dt: datetime, minutes: int = ROUND_MINUTES) -> datetime:
    dt0 = dt.replace(second=0, microsecond=0)
    remainder = dt0.minute % minutes
    if remainder == 0:
        return dt0
    return dt0 - timedelta(minutes=remainder)


def parse_month_str(month_str: str) -> tuple[int, int]:
    try:
        year, month = month_str.split("-")
        y = int(year)
        m = int(month)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="month は YYYY-MM 形式で指定してください") from exc

    if not 1 <= m <= 12:
        raise HTTPException(status_code=400, detail="month は YYYY-MM 形式で指定してください")

    return y, m


def format_month_str(year: int, month: int) -> str:
    return f"{year}-{str(month).zfill(2)}"


def month_range(month: str) -> tuple[datetime, datetime]:
    year, month_num = parse_month_str(month)
    start = datetime(year, month_num, 1)
    if month_num == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month_num + 1, 1)
    return start, end


def get_previous_month_str(base_dt: datetime | None = None) -> str:
    current = base_dt or now_jst_naive()
    year = current.year
    month_num = current.month - 1
    if month_num == 0:
        year -= 1
        month_num = 12
    return format_month_str(year, month_num)


def _today_range() -> tuple[datetime, datetime]:
    today = now_jst_naive().date()
    start = datetime.combine(today, time.min)
    end = start + timedelta(days=1)
    return start, end


def _ensure_workday(db: Session, user_id: int, target_date: date) -> Workday:
    workday = (
        db.query(Workday)
        .filter(Workday.user_id == user_id, Workday.date == target_date)
        .one_or_none()
    )
    if workday is None:
        workday = Workday(
            user_id=user_id,
            date=target_date,
            status="open",
            created_at=now_jst_naive(),
            updated_at=now_jst_naive(),
        )
        db.add(workday)
        db.flush()
    return workday


def _is_session_valid(row: DbSession, now: datetime, idle_timeout: timedelta | None) -> bool:
    if row.revoked or row.expires_at <= now:
        return False
    if idle_timeout and row.last_seen_at and (now - row.last_seen_at) > idle_timeout:
        return False
    return True


def _revoke_all_user_sessions(db: Session, user_id: int) -> None:
    db.query(DbSession).filter(
        DbSession.user_id == user_id,
        DbSession.revoked.is_(False),
        DbSession.device_label.like("mobile_%"),
    ).update({DbSession.revoked: True}, synchronize_session=False)
    db.commit()


def _revoke_session(db: Session, row: DbSession | None) -> None:
    if row is None or row.revoked:
        return
    row.revoked = True
    db.commit()


def _issue_session_token(
    db: Session,
    *,
    user_id: int,
    ttl: timedelta,
    label_prefix: str,
    device_name: str | None,
) -> str:
    token = new_session_token()
    now = now_jst_naive()
    session = DbSession(
        id=_hash_token(token),
        user_id=user_id,
        created_at=now,
        expires_at=now + ttl,
        last_seen_at=now,
        revoked=False,
        device_label=f"{label_prefix}:{(device_name or 'mobile').strip()[:80]}",
    )
    db.add(session)
    db.commit()
    return token


def _resolve_bearer_session(
    db: Session,
    creds: HTTPAuthorizationCredentials | None,
    *,
    expected_prefix: str,
) -> DbSession:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="認証が必要です")

    row = db.query(DbSession).filter(DbSession.id == _hash_token(creds.credentials)).one_or_none()
    if row is None or not (row.device_label or "").startswith(expected_prefix + ":"):
        raise HTTPException(status_code=401, detail="トークンが無効です")

    now = now_jst_naive()
    if not _is_session_valid(row, now, ACCESS_IDLE_TIMEOUT):
        _revoke_session(db, row)
        raise HTTPException(status_code=401, detail="トークンの有効期限が切れています")

    row.last_seen_at = now
    db.commit()
    return row


def get_mobile_access_session(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> DbSession:
    return _resolve_bearer_session(db, creds, expected_prefix=MOBILE_ACCESS_LABEL)


def get_mobile_current_user(
    access_session: DbSession = Depends(get_mobile_access_session),
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(User.id == access_session.user_id, User.is_active.is_(True)).one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="ユーザーが無効です")
    return user


def get_month_confirmation(db: Session, user_id: int, month_str: str) -> MonthlyConfirmation | None:
    year, month = parse_month_str(month_str)
    return (
        db.query(MonthlyConfirmation)
        .filter(
            MonthlyConfirmation.user_id == user_id,
            MonthlyConfirmation.year == year,
            MonthlyConfirmation.month == month,
        )
        .one_or_none()
    )


def get_required_hours(db: Session, user_id: int, month_str: str) -> float:
    year, month_num = parse_month_str(month_str)
    monthly_requirement = (
        db.query(UserMonthlyRequirement)
        .filter(
            UserMonthlyRequirement.user_id == user_id,
            UserMonthlyRequirement.year == year,
            UserMonthlyRequirement.month == month_num,
        )
        .one_or_none()
    )
    if monthly_requirement is not None:
        return monthly_requirement.required_hours

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).one_or_none()
    if user and user.required_hours is not None:
        return user.required_hours

    return DEFAULT_REQUIRED_HOURS


def group_logs_by_workday_start(logs: list[AttendanceLog]) -> dict[date, list[AttendanceLog]]:
    grouped: dict[date, list[AttendanceLog]] = {}
    current_shift: list[AttendanceLog] = []
    current_start_date: date | None = None
    in_shift = False

    for log in sorted(logs, key=lambda row: row.ts):
        if log.action == ACTION_IN:
            if in_shift and current_start_date is not None:
                grouped.setdefault(current_start_date, []).extend(current_shift)
            current_shift = [log]
            current_start_date = log.ts.date()
            in_shift = True
            continue

        if in_shift:
            current_shift.append(log)

        if log.action == ACTION_OUT:
            if current_start_date is not None:
                grouped.setdefault(current_start_date, []).extend(current_shift)
            current_shift = []
            current_start_date = None
            in_shift = False

    if in_shift and current_start_date is not None:
        grouped.setdefault(current_start_date, []).extend(current_shift)

    return grouped


def calc_day_from_logs(day_logs: list[AttendanceLog], break_rule: str = BREAK_RULE_STANDARD) -> dict:
    if not day_logs:
        return {
            "ok": False,
            "gross_sec": 0,
            "break_sec": 0,
            "net_sec": 0,
            "error": "ログなし",
            "start": None,
            "end": None,
        }

    sorted_logs = sorted(day_logs, key=lambda item: item.ts)
    ins = [log.ts for log in sorted_logs if log.action == ACTION_IN]
    outs = [log.ts for log in sorted_logs if log.action == ACTION_OUT]

    if not ins or not outs:
        return {
            "ok": False,
            "gross_sec": 0,
            "break_sec": 0,
            "net_sec": 0,
            "error": "入室または退室が不足",
            "start": min(ins) if ins else None,
            "end": max(outs) if outs else None,
        }

    raw_start = min(ins)
    raw_end = max(outs)
    start_dt = ceil_time(raw_start)
    end_dt = floor_time(raw_end)

    if end_dt < start_dt:
        return {
            "ok": False,
            "gross_sec": 0,
            "break_sec": 0,
            "net_sec": 0,
            "error": "入退室時刻が不正",
            "start": start_dt,
            "end": end_dt,
        }

    gross_sec = max(int((end_dt - start_dt).total_seconds()), 0)
    if break_rule == BREAK_RULE_ALWAYS_1H:
        break_sec = 3600 if gross_sec > 0 else 0
    else:
        break_sec = 3600 if gross_sec >= 6 * 3600 else 0

    net_sec = max(gross_sec - break_sec, 0)
    return {
        "ok": True,
        "gross_sec": gross_sec,
        "break_sec": break_sec,
        "net_sec": net_sec,
        "error": None,
        "start": start_dt,
        "end": end_dt,
    }


def calc_month_summary_from_logs(
    logs: list[AttendanceLog],
    month: str,
    required_hours: float,
    break_rule: str = BREAK_RULE_STANDARD,
) -> dict:
    day_map = group_logs_by_workday_start(logs)

    worked_days = 0
    inconsistent_days = 0
    total_net_sec = 0

    for target_date, day_logs in day_map.items():
        if not str(target_date).startswith(month):
            continue

        worked_days += 1
        result = calc_day_from_logs(day_logs, break_rule)
        if result["ok"]:
            total_net_sec += result["net_sec"]
        else:
            inconsistent_days += 1

    required_sec = int(required_hours * 3600)
    remaining_sec = max(required_sec - total_net_sec, 0)

    return {
        "month": month,
        "worked_days": worked_days,
        "required_hours": required_hours,
        "worked_time_text": sec_to_hm(total_net_sec),
        "remaining_time_text": sec_to_hm(remaining_sec),
        "inconsistent_days": inconsistent_days,
    }


def build_previous_month_days(
    db: Session,
    *,
    user: User,
    month: str,
    logs: list[AttendanceLog],
) -> list[dict]:
    start, end = month_range(month)
    workdays = (
        db.query(Workday)
        .filter(
            Workday.user_id == user.id,
            Workday.date >= start.date(),
            Workday.date < end.date(),
        )
        .all()
    )
    workday_map = {row.date: row for row in workdays}
    day_map = group_logs_by_workday_start(logs)
    all_dates = sorted(set(day_map.keys()) | set(workday_map.keys()))

    rows: list[dict] = []
    for target_date in all_dates:
        day_logs = day_map.get(target_date, [])
        workday = workday_map.get(target_date)
        employee_note = workday.employee_note if workday else None
        result = calc_day_from_logs(day_logs, user.break_rule or BREAK_RULE_STANDARD) if day_logs else None

        rows.append(
            {
                "date": str(target_date),
                "start_at": result["start"].strftime("%H:%M") if result and result["start"] else None,
                "end_at": result["end"].strftime("%H:%M") if result and result["end"] else None,
                "worked_time_text": sec_to_hm(result["net_sec"]) if result and result["ok"] else "-",
                "note": employee_note or (result["error"] if result and result["error"] else ""),
                "status": "complete" if result and result["ok"] else "incomplete",
            }
        )

    return rows


def _serialize_user(user: User) -> dict:
    return {
        "id": str(user.id),
        "login_id": user.name,
        "display_name": user.name,
        "role": "admin" if user.role == "admin" else "member",
        "organization_name": "Attendance Mobile",
        "department_name": "General",
        "break_rule": user.break_rule or BREAK_RULE_STANDARD,
    }


def _last_attendance_log(db: Session, user_id: int) -> AttendanceLog | None:
    return (
        db.query(AttendanceLog)
        .filter(AttendanceLog.user_id == user_id, AttendanceLog.action.in_(ACTIONS))
        .order_by(AttendanceLog.ts.desc())
        .first()
    )


def _today_logs(db: Session, user_id: int) -> list[AttendanceLog]:
    start, end = _today_range()
    return (
        db.query(AttendanceLog)
        .filter(
            AttendanceLog.user_id == user_id,
            AttendanceLog.ts >= start,
            AttendanceLog.ts < end,
            AttendanceLog.action.in_(ACTIONS),
        )
        .order_by(AttendanceLog.ts.asc())
        .all()
    )


def _serialize_timeline(logs: list[AttendanceLog]) -> list[dict]:
    items: list[dict] = []
    for log in sorted(logs, key=lambda row: row.ts, reverse=True):
        items.append(
            {
                "id": str(log.id),
                "action": "clock_in" if log.action == ACTION_IN else "clock_out",
                "action_label": log.action,
                "occurred_at": log.ts.isoformat(),
                "source": "mobile" if (log.source or "mobile") == "mobile" else "manual",
                "location": {
                    "latitude": log.lat,
                    "longitude": log.lon,
                    "accuracy": None,
                }
                if log.lat is not None and log.lon is not None
                else None,
            }
        )
    return items


def build_dashboard_response(db: Session, user: User) -> dict:
    now = now_jst_naive()
    today_logs = _today_logs(db, user.id)
    today_workday = _ensure_workday(db, user.id, now.date()) if today_logs else (
        db.query(Workday)
        .filter(Workday.user_id == user.id, Workday.date == now.date())
        .one_or_none()
    )

    last_log = _last_attendance_log(db, user.id)
    if last_log is None or last_log.action == ACTION_OUT:
        state = STATE_NOT_IN
    elif last_log.action == ACTION_IN:
        state = STATE_IN_ROOM
    else:
        state = STATE_UNKNOWN

    work_time = calc_day_from_logs(today_logs, user.break_rule or BREAK_RULE_STANDARD)
    work_time_text = sec_to_hm(work_time["net_sec"]) if work_time["ok"] else "0時間0分"
    gross_work_time_text = sec_to_hm(work_time["gross_sec"]) if today_logs else "0時間0分"
    break_time_text = sec_to_hm(work_time["break_sec"]) if today_logs else "0時間0分"

    current_month = format_month_str(now.year, now.month)
    month_logs = (
        db.query(AttendanceLog)
        .filter(
            AttendanceLog.user_id == user.id,
            AttendanceLog.ts >= month_range(current_month)[0],
            AttendanceLog.ts < month_range(current_month)[1],
            AttendanceLog.action.in_(ACTIONS),
        )
        .order_by(AttendanceLog.ts.asc())
        .all()
    )
    month_summary = calc_month_summary_from_logs(
        month_logs,
        current_month,
        get_required_hours(db, user.id, current_month),
        user.break_rule or BREAK_RULE_STANDARD,
    )

    previous_month = get_previous_month_str(now)
    previous_month_logs = (
        db.query(AttendanceLog)
        .filter(
            AttendanceLog.user_id == user.id,
            AttendanceLog.ts >= month_range(previous_month)[0],
            AttendanceLog.ts < month_range(previous_month)[1],
            AttendanceLog.action.in_(ACTIONS),
        )
        .order_by(AttendanceLog.ts.asc())
        .all()
    )
    previous_month_summary = calc_month_summary_from_logs(
        previous_month_logs,
        previous_month,
        get_required_hours(db, user.id, previous_month),
        user.break_rule or BREAK_RULE_STANDARD,
    )
    confirmation = get_month_confirmation(db, user.id, previous_month)

    return {
        "user": _serialize_user(user),
        "today": {
            "date": str(now.date()),
            "state": state,
            "state_label": "在室中" if state == STATE_IN_ROOM else "未入室 / 退室済み" if state == STATE_NOT_IN else "状態不明",
            "last_action": None if last_log is None else ("clock_in" if last_log.action == ACTION_IN else "clock_out"),
            "last_clock_at": last_log.ts.isoformat() if last_log else None,
            "note": today_workday.employee_note if today_workday and today_workday.employee_note else "",
            "note_updated_at": today_workday.updated_at.isoformat() if today_workday else None,
            "location": {
                "latitude": last_log.lat if last_log else None,
                "longitude": last_log.lon if last_log else None,
                "text": f"{last_log.lat:.5f}, {last_log.lon:.5f}" if last_log and last_log.lat is not None and last_log.lon is not None else "位置情報なし",
            },
            "work_time_text": work_time_text,
            "gross_work_time_text": gross_work_time_text,
            "break_time_text": break_time_text,
            "first_clock_at": today_logs[0].ts.isoformat() if today_logs else None,
            "can_clock_in": state != STATE_IN_ROOM,
            "can_clock_out": state == STATE_IN_ROOM,
            "worked_minutes": work_time["net_sec"] // 60 if work_time["ok"] else 0,
        },
        "timeline": _serialize_timeline(today_logs),
        "month_summary": month_summary,
        "previous_month": {
            "month": previous_month,
            "summary": previous_month_summary,
            "confirmed_at": confirmation.confirmed_at.isoformat() if confirmation else None,
            "confirmed_by": confirmation.confirmed_name if confirmation else None,
            "can_confirm": confirmation is None,
            "days": build_previous_month_days(
                db,
                user=user,
                month=previous_month,
                logs=previous_month_logs,
            ),
        },
        "business_date_label": f"{now.year}年{now.month}月{now.day}日",
        "last_synced_at": now.isoformat(),
    }


@mobile_v1_router.post("/auth/login")
def mobile_login(body: MobileLoginRequest, request: Request, db: Session = Depends(get_db)):
    login_id = body.login_id.strip()
    pin = body.pin.strip()
    ip = request.client.host if request.client else "unknown"
    now = now_jst_naive()

    if login_blocked(login_id, ip, now):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=LOGIN_BLOCKED_MESSAGE)

    user = db.query(User).filter(User.name == login_id, User.is_active.is_(True)).one_or_none()
    if not verify_pin_or_dummy(pin, user.pin_hash if user else None):
        record_login_failure(login_id, ip, now)
        raise HTTPException(status_code=401, detail="ログインIDまたはPINが正しくありません")

    clear_login_failures(login_id)

    access_token = _issue_session_token(
        db,
        user_id=user.id,
        ttl=ACCESS_TOKEN_TTL,
        label_prefix=MOBILE_ACCESS_LABEL,
        device_name=body.device_name,
    )
    refresh_token = _issue_session_token(
        db,
        user_id=user.id,
        ttl=REFRESH_TOKEN_TTL,
        label_prefix=MOBILE_REFRESH_LABEL,
        device_name=body.device_name,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "user": _serialize_user(user),
    }


@mobile_v1_router.post("/auth/refresh")
def mobile_refresh(body: MobileRefreshRequest, db: Session = Depends(get_db)):
    refresh_session = (
        db.query(DbSession)
        .filter(DbSession.id == _hash_token(body.refresh_token))
        .one_or_none()
    )
    if refresh_session is None or not (refresh_session.device_label or "").startswith(MOBILE_REFRESH_LABEL + ":"):
        raise HTTPException(status_code=401, detail="refresh_token が無効です")

    now = now_jst_naive()
    if refresh_session.revoked and refresh_session.expires_at > now:
        # 使用済みリフレッシュトークンの再利用 = 漏洩の可能性。全セッションを失効させる
        _revoke_all_user_sessions(db, refresh_session.user_id)
        raise HTTPException(status_code=401, detail="refresh_token が無効です")

    if not _is_session_valid(refresh_session, now, REFRESH_IDLE_TIMEOUT):
        _revoke_session(db, refresh_session)
        raise HTTPException(status_code=401, detail="refresh_token の有効期限が切れています")

    user = db.query(User).filter(User.id == refresh_session.user_id, User.is_active.is_(True)).one_or_none()
    if user is None:
        _revoke_session(db, refresh_session)
        raise HTTPException(status_code=401, detail="ユーザーが無効です")

    device_name = (refresh_session.device_label or "").split(":", 1)[-1]
    _revoke_session(db, refresh_session)

    access_token = _issue_session_token(
        db,
        user_id=user.id,
        ttl=ACCESS_TOKEN_TTL,
        label_prefix=MOBILE_ACCESS_LABEL,
        device_name=device_name,
    )
    next_refresh_token = _issue_session_token(
        db,
        user_id=user.id,
        ttl=REFRESH_TOKEN_TTL,
        label_prefix=MOBILE_REFRESH_LABEL,
        device_name=device_name,
    )

    return {
        "access_token": access_token,
        "refresh_token": next_refresh_token,
        "token_type": "bearer",
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "user": _serialize_user(user),
    }


@mobile_v1_router.post("/auth/logout")
def mobile_logout(
    body: MobileLogoutRequest,
    access_session: DbSession = Depends(get_mobile_access_session),
    db: Session = Depends(get_db),
):
    _revoke_session(db, access_session)

    if body.refresh_token:
        refresh_session = (
            db.query(DbSession)
            .filter(DbSession.id == _hash_token(body.refresh_token))
            .one_or_none()
        )
        if refresh_session and refresh_session.user_id == access_session.user_id:
            _revoke_session(db, refresh_session)

    return {"ok": True}


@mobile_v1_router.get("/me")
def mobile_me(user: User = Depends(get_mobile_current_user)):
    return _serialize_user(user)


@mobile_v1_router.get("/dashboard")
def mobile_dashboard(
    user: User = Depends(get_mobile_current_user),
    db: Session = Depends(get_db),
):
    return build_dashboard_response(db, user)


@mobile_v1_router.post("/attendance/clock")
def mobile_clock(
    body: MobileClockRequest,
    user: User = Depends(get_mobile_current_user),
    db: Session = Depends(get_db),
):
    last_log = _last_attendance_log(db, user.id)

    if body.action == "clock_in":
        if last_log is not None and last_log.action != ACTION_OUT:
            raise HTTPException(status_code=400, detail="すでに入室しています")
        action_label = ACTION_IN
        next_status = "open"
    else:
        if last_log is None or last_log.action != ACTION_IN:
            raise HTTPException(status_code=400, detail="入室していないため退室できません")
        action_label = ACTION_OUT
        next_status = "closed"

    now = now_jst_naive()
    log = AttendanceLog(
        user_id=user.id,
        action=action_label,
        ts=now,
        lat=body.location.latitude if body.location else None,
        lon=body.location.longitude if body.location else None,
        source="mobile",
    )
    db.add(log)

    workday = _ensure_workday(db, user.id, now.date())
    workday.status = next_status
    workday.updated_at = now_jst_naive()
    db.commit()

    return build_dashboard_response(db, user)


@mobile_v1_router.put("/attendance/today-note")
def mobile_save_today_note(
    body: MobileTodayNoteRequest,
    user: User = Depends(get_mobile_current_user),
    db: Session = Depends(get_db),
):
    today = now_jst_naive().date()
    workday = _ensure_workday(db, user.id, today)
    workday.employee_note = body.note[:500] or None
    workday.updated_at = now_jst_naive()
    db.commit()
    return build_dashboard_response(db, user)


@mobile_v1_router.post("/attendance/months/{month}/confirm")
def mobile_confirm_month(
    month: str,
    user: User = Depends(get_mobile_current_user),
    db: Session = Depends(get_db),
):
    parse_month_str(month)

    confirmation = get_month_confirmation(db, user.id, month)
    if confirmation is None:
        year, month_num = parse_month_str(month)
        confirmation = MonthlyConfirmation(
            user_id=user.id,
            year=year,
            month=month_num,
            confirmed_at=now_jst_naive(),
            confirmed_name=user.name,
        )
        db.add(confirmation)
        db.commit()

    return build_dashboard_response(db, user)
