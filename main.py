import os, csv, io
from urllib.parse import quote
from datetime import datetime, date, time, timedelta
import secrets
import pandas as pd
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi import Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db import Session as DbSession, SessionLocal, init_db, User, AttendanceLog, Workday, init_engine, Base, engine
from security import verify_pin
from sqlalchemy import or_
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from collections import defaultdict


SESSION_RETENTION_DAYS = 30

def cleanup_sessions(db: Session) -> int:
  now = datetime.now()
  cutoff = now - timedelta(days=SESSION_RETENTION_DAYS)

  q = (
    db.query(DbSession)
    .filter(
      or_(
        DbSession.expires_at < now,
        DbSession.revoked == True,
      ),
      DbSession.created_at < cutoff
    )
  )

  deleted = q.delete(synchronize_session=False)
  db.commit()
  return deleted
app = FastAPI()

@app.on_event("startup")
def startup():
    init_db()
    with SessionLocal() as db:
      cleanup_sessions(db)

DEFAULT_USER = "瀬良 仁"
SESSION_TTL_HOURS = 8          # 絶対期限
IDLE_TIMEOUT_MINUTES = 10   # 無操作タイムアウト（共有端末向け）


bearer_scheme = HTTPBearer(auto_error=False)



STATIC_DIR = "static"
INDEX_FILE = os.path.join(STATIC_DIR, "index.html")
ADMIN_FILE = os.path.join(STATIC_DIR, "admin.html")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



@app.get("/admin")
def admin_page():
  if os.path.exists(ADMIN_FILE):
    return FileResponse(ADMIN_FILE)
  raise HTTPException(status_code=404, detail="admin.html not found")

def get_db():
  db = SessionLocal()
  try:
    yield db
  finally:
    db.close()

ACTIONS = {"出勤", "退勤", "休憩開始", "休憩終了"}


def _get_user_id(db: Session, user_name: str) -> int | None:
  u = db.query(User).filter(User.name == user_name, User.is_active == True).one_or_none()
  return u.id if u else None

def get_acurrent_user_row(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme), db: Session = Depends(get_db)) -> User | None:
  if not creds:
    print("[auth] no credentials")
    return None
  
  session_id = creds.credentials

  now = datetime.now()
  s = db.query(DbSession).filter(DbSession.id == session_id).one_or_none()
  if not s:
    print("[auth] session not found:", session_id)
    return None
  if s.revoked:
    print("[auth] session revoked:", session_id)
    return None
  if s.expires_at <= now:
    print("[auth] session expired:", session_id, "expires_at=", s.expires_at, "now=", now)
    s.revoked = True
    db.commit()
    return None
  
  if s.last_seen_at and (now - s.last_seen_at) > timedelta(minutes=IDLE_TIMEOUT_MINUTES):
    print("[auth] idle timeout:", session_id, "last_seen_at=", s.last_seen_at, "now=", now)
    s.revoked = True
    db.commit()
    return None
  
  s.last_seen_at = now
  db.commit()

  u = db.query(User).filter(User.id == s.user_id, User.is_active == True).one_or_none()
  if not u:
    print("[auth] user not found or inactive:", s.user_id)
  return u

def require_admin(user: User | None = Depends(get_acurrent_user_row)) -> User:
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")
  if user.role != "admin":
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="権限がありません")
  return user

def month_range(month: str) -> tuple[datetime, datetime]:
  y, m = month.split("-")
  year = int(y)
  mon = int(m)
  start = datetime(year, mon, 1)
  
  if mon == 12:
    end = datetime(year+1, 1, 1)
  else:
    end = datetime(year, mon+1, 1)
  return start, end

def _today_range() -> tuple[datetime, datetime]:
  today = datetime.now().date()
  start = datetime.combine(today, time.min)
  end = start + timedelta(days=1)
  return start, end

def _ensure_workday(db: Session, user_id: int, d:date) -> Workday:
  wd = db.query(Workday).filter(Workday.user_id == user_id, Workday.date == d).one_or_none()
  if wd is None:
    wd = Workday(user_id=user_id, date=d, status="open", created_at=datetime.now(), updated_at=datetime.now())
    db.add(wd)
    db.flush()
  return wd

def add_log_db(db: Session, action: str, user_name: str, lat: float | None = None, lon: float | None = None):
  if action not in ACTIONS:
    raise ValueError("unknown action")
  
  user_id = _get_user_id(db, user_name)
  if user_id is None:
    raise ValueError("unknown user")
  
  now_dt = datetime.now()

  log = AttendanceLog(user_id=user_id, action=action, ts=now_dt, lat=lat, lon=lon, source=None)
  db.add(log)

  wd = _ensure_workday(db, user_id, now_dt.date())

  if action == "出勤":
    wd.status = "open"
  elif action == "退勤":
    wd.status = "closed"
  wd.updated_at = datetime.now()

  db.commit()

  return {
    "ユーザー": user_name,
    "アクション": action,
    "時刻": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
    "緯度": lat,
    "経度": lon,
  }

def get_last_action_db(db: Session, user_name:str) -> str | None:
  user_id = _get_user_id(db, user_name)
  if user_id is None:
    return None
  
  last = (
    db.query(AttendanceLog).filter(AttendanceLog.user_id == user_id)
    .order_by(AttendanceLog.ts.desc())
    .first()
  )
  return last.action if last else None

def get_today_logs_db(db: Session, user_name: str):
  user_id = _get_user_id(db, user_name)
  if user_id is None:
    return []
  
  start, end = _today_range()
  logs = (db.query(AttendanceLog)
          .filter(AttendanceLog.user_id == user_id, AttendanceLog.ts >= start, AttendanceLog.ts<end)
          .order_by(AttendanceLog.ts.asc())
          .all()
          )
  
  return [
    {
      "ユーザー": user_name,
      "アクション": l.action,
      "時刻": l.ts.strftime("%Y-%m-%d %H:%M:%S"),
      "緯度": l.lat,
      "経度": l.lon,
    }
    for l in logs
  ]

def get_current_state_db(db: Session, user_name: str):
  user_id = _get_user_id(db, user_name)
  if user_id is None:
    return {"state": "未出勤", "last_action": None, "lat": None, "lon": None, "time": None}
  
  last = (
    db.query(AttendanceLog)
    .filter(AttendanceLog.user_id == user_id)
    .order_by(AttendanceLog.ts.desc())
    .first()
  )

  if not last:
    return {"state": "未出勤", "last_action": None, "lat": None, "lon": None, "time": None}
  
  last_action = last.action

  if last_action is None or last_action == "退勤":
    state = "未出勤"
  elif last_action in ("出勤", "休憩終了"):
    state = "出勤中"
  elif last_action == "休憩開始":
    state = "休憩中"
  else:
    state = "不明"

  last_time = last.ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(last.ts, "strftime") else (str(last.ts) if last.ts is not None else None)
  return {
    "state" : state,
    "last_action": last_action,
    "lat": last.lat,
    "lon": last.lon,
    "time": last_time,
  }

def group_logs_by_workday_start(logs: list["AttendanceLog"]) -> dict[date, list["AttendanceLog"]]:
  by_day: dict[date, list[AttendanceLog]] = defaultdict(list)

  current: list[AttendanceLog] = []
  start_day: date | None = None
  in_shift = False

  for l in logs:
    if l.action == "出勤":
      if in_shift and start_day is not None:
        by_day[start_day].extend(current)
      current = [l]
      start_day = l.ts.date()
      in_shift = True
      continue

    if in_shift:
      current.append(l)

    if l.action == "退勤":
      if start_day is not None:
        by_day[start_day].extend(current)
      current = []
      start_day = None
      in_shift = False

  if in_shift and start_day is not None:
    by_day[start_day].extend(current)

  return dict(by_day)

def calc_work_time_db(db: Session, user_name: str):
  logs = get_today_logs_db(db, user_name)
  if not logs:
    return {"error": "記録がありません"}
  
  parsed = []
  for l in logs:
    ts_val = l["時刻"]
    ts = ts_val if isinstance(ts_val, datetime) else datetime.strptime(ts_val, "%Y-%m-%d %H:%M:%S")
    parsed.append((l["アクション"], ts))

  break_start = [ts for a, ts in parsed if a == "休憩開始"]
  break_end = [ts for a, ts in parsed if a == "休憩終了"]

  if len(break_start) != len(break_end):
    return {"error": "休憩開始・終了が一致していません"}
  
  total_break = timedelta(0)
  for s, e in zip(break_start, break_end):
    if e < s:
      return {"error": "休憩の時刻が不正です"}
    total_break += (e - s)

  clock_ins = [ts for a, ts in parsed if a == "出勤"]
  clock_outs = [ts for a, ts in parsed if a == "退勤"]
  if not clock_ins or not clock_outs:
    return {"error": "出勤または退勤の記録がありません"}
  
  start = min(clock_ins)
  end = max(clock_outs)
  if end < start:
    return {"error": "出退勤の時刻が不正です"}
  
  gross = end - start
  net = gross - total_break
  total_minutes = int(net.total_seconds() // 60)
  hours = total_minutes // 60
  minutes = total_minutes % 60

  today = datetime.now().date()
  return {
    "date": str(today),
    "gross_work_time": str(gross),
    "break_time": str(total_break),
    "net_work_time": f"{hours}時間{minutes}分"
  }
    
def calc_day_from_logs(day_logs: list[AttendanceLog]) -> dict:
  if not day_logs:
    return {"ok": False, "gross_sec": 0, "break_sec": 0, "net_sec": 0, "error": "ログなし", "start": None, "end": None}
  
  day_logs = sorted(day_logs, key=lambda x : x.ts)

  ins = [l.ts for l in day_logs if l.action == "出勤"]
  outs = [l.ts for l in day_logs if l.action == "退勤"]
  if not ins or not outs:
    return {"ok": False, "gross_sec": 0, "break_sec": 0, "net_sec": 0, "error": "出勤または退勤が不足", "start": (min(ins) if ins else None), "end": (max(outs) if outs else None)}
  
  start = min(ins)
  end = max(outs)
  if end < start:
    return {"ok": False, "gross_sec": 0, "break_sec": 0, "net_sec": 0, "error": "出退勤時刻が不正", "start": start, "end": end}
  
  bs = [l.ts for l in day_logs if l.action == "休憩開始"]
  be = [l.ts for l in day_logs if l.action == "休憩終了"]
  if len(bs) != len(be):
    return {"ok": False, "gross_sec": int((end - start).total_seconds()), "break_sec": 0, "net_sec": 0, "error": "休憩開始・終了が不一致", "start": start, "end": end}
  
  break_sec = 0
  for s, e in zip(bs, be):
    if e < s:
      return {"ok": False, "gross_sec": int((end - start).total_seconds()), "break_sec": 0, "net_sec": 0, "error": "休憩時刻が不正", "start": start, "end": end}
    break_sec += int((e - s).total_seconds())

  gross_sec = int((end-start).total_seconds())
  net_sec = gross_sec - break_sec
  if net_sec<0:
    return {"ok": False, "gross_sec": gross_sec, "break_sec": break_sec, "net_sec": 0, "error": "休憩が勤務を超過", "start": start, "end": end}
  return {"ok": True, "gross_sec": gross_sec, "break_sec": break_sec, "net_sec": net_sec, "error": None, "start": start, "end": end}

def sec_to_hm(sec: int) ->str:
  minutes = sec // 60
  h = minutes // 60
  m = minutes % 60
  return f"{h}時間{m}分"

def _get_bearer_token(authorization: str | None) -> str | None:
  if not authorization:
    return None
  elif not authorization.startswith("Bearer "):
    return None
  return authorization.removeprefix("Bearer ").strip() or None

class LoginRequest(BaseModel):
  user: str
  pin:str

def get_current_user(authorization: str = Header(None), db: Session = Depends(get_db)):
  session_id = _get_bearer_token(authorization)
  if not session_id:
    return None
  
  now = datetime.now()

  s = (
    db.query(DbSession).filter(DbSession.id == session_id).one_or_none()
  )

  if not s:
    return None
  if s.revoked:
    return None
  if s.expires_at <= now:
    s.revoked = True
    db.commit()
    return None
  
  if s.last_seen_at and now - s.last_seen_at > timedelta(minutes=IDLE_TIMEOUT_MINUTES):
    s.revoked = True
    db.commit()
    return None
  
  s.last_seen_at = now
  db.commit()

  u = db.query(User).filter(User.id == s.user_id, User.is_active == True).one_or_none()
  return u.name if u else None


@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
  user_name = req.user.strip()
  pin = req.pin.strip()

  u = db.query(User).filter(User.name == user_name, User.is_active == True).one_or_none()
  if not u:
    return {"ok": False, "error": "認証に失敗しました"}
  
  if not verify_pin(pin, u.pin_hash):
    return {"ok": False, "error": "認証に失敗しました"}
  
  session_id = secrets.token_hex(16)
  now = datetime.now()

  s = DbSession(
    id=session_id,
    user_id=u.id,
    created_at=now,
    expires_at=now + timedelta(hours=SESSION_TTL_HOURS),
    last_seen_at=now,
    revoked=False,
    device_label=None
  )
  db.add(s)
  db.commit()

  return {"ok": True, "session": session_id, "user": u.name, "role": u.role}

@app.get("/admin/ping")
def admin_ping(admin: User = Depends(require_admin)):
  return {"ok": True, "user": admin.name}



@app.post("/logout")
def logout(authorization: str | None = Header(None), db: Session = Depends(get_db),):
  session_id = _get_bearer_token(authorization)
  if not session_id:
    return {"ok": True}
  
  s = db.query(DbSession).filter(DbSession.id == session_id).one_or_none()
  if s:
    s.revoked = True
    db.commit()
  return {"ok": True}

@app.get("/")
def root():
  if os.path.exists(INDEX_FILE):
    return FileResponse(INDEX_FILE)
  return {"message": "Attendance API is running"}

@app.get("/clock-in")
def clock_in(user: str = Depends(get_current_user), lat: float = None, lon: float = None, db: Session = Depends(get_db),):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")
  
  try:
    last = get_last_action_db(db, user)
    if last != "退勤" and last is not None:
      return {"error": "すでに出勤しています"}
      
    row = add_log_db(db, "出勤", user, lat, lon)
    return {"status": "ok", "data": row}
  
  except Exception:
    return {"error":"出勤の記録に失敗しました。"}

  
@app.get("/clock-out")
def clock_out(user: str = Depends(get_current_user), lat: float = None, lon: float = None, db: Session=Depends(get_db),):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")
  try:
    last = get_last_action_db(db, user)
    if last != "出勤" and last != "休憩終了":
      return {"error": "出勤していないため退勤できません"}
    
    row = add_log_db(db, "退勤", user, lat, lon)
    return {"status": "ok", "data": row}
  except Exception:
    return {"error": "退勤の記録に失敗しました"}

@app.get("/break-start")
def break_start(user: str = Depends(get_current_user), lat: float = None, lon: float = None, db: Session = Depends(get_db),):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")

  try:
    last = get_last_action_db(db, user)
    if last != "出勤" and last != "休憩終了":
      return {"error": "勤務中でなければ休憩に入れません"}
    
    row = add_log_db(db, "休憩開始", user, lat, lon)
    return {"status": "ok", "data": row}
  except Exception:
    return {"error": "休憩開始の記録に失敗しました"}

@app.get("/break-end")
def break_end(user: str = Depends(get_current_user), lat: float = None, lon: float = None, db: Session = Depends(get_db),):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")

  try:
    last = get_last_action_db(db, user)
    if last != "休憩開始":
      return {"error": "休憩中でなければ休憩終了できません"}
    
    row = add_log_db(db, "休憩終了", user, lat, lon)
    return {"status": "ok", "data": row}
  except Exception:
    return {"error": "休憩終了の記録に失敗しました"}

@app.get("/today-logs")
def today_logs(user: str = Depends(get_current_user), db: Session = Depends(get_db)):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")

  try:
    return {"logs": get_today_logs_db(db, user)}
  except Exception:
    return {"error": "ログの取得に失敗しました", "logs": []}

@app.get("/current-state")
def current_state(user: str = Depends(get_current_user), db: Session = Depends(get_db),):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")

  try:
    return get_current_state_db(db, user)
  except Exception:
    return {"error": "状態の取得に失敗しました", "state": "不明"}

@app.get("/work-time")
def work_time(user: str = Depends(get_current_user), db: Session = Depends(get_db)):
  if user is None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未ログイン")

  try:
    return calc_work_time_db(db, user)
  except Exception:
    return {"error": "勤務時間の計算に失敗しました"}

@app.get("/admin/month-summary")
def admin_month_summary(month, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
  start, end = month_range(month)

  users = db.query(User).filter(User.is_active == True).order_by(User.id.asc()).all()

  logs = (
    db.query(AttendanceLog)
    .filter(AttendanceLog.ts >= start, AttendanceLog.ts < (end + timedelta(days=1)))
    .order_by(AttendanceLog.user_id.asc(), AttendanceLog.ts.asc())
    .all()
  )

  by_user_day: dict[int, dict[date, list[AttendanceLog]]] = {}
  for l in logs:
    by_user_day.setdefault(l.user_id, {}).setdefault(l.ts.date(), []).append(l)

  wds = (
    db.query(Workday)
    .filter(Workday.date >= start.date(), Workday.date < end.date())
    .all()
    )
  
  wd_map : dict[tuple[int, date]: Workday] = {(w.user_id, w.date): w for w in wds}

  rows = []

  for u in users:
    user_logs = [
      l for day_logs in by_user_day.get(u.id, {}).values() for l in day_logs
    ]
    day_map = group_logs_by_workday_start(user_logs)
    work_days = 0
    ok_days = 0
    inconsistent_day = 0
    open_days = 0

    total_gross = 0
    total_break = 0
    total_net = 0

    for d, day_logs in day_map.items():
      if not (start.date() <= d < end.date()):
        continue

      work_days += 1

      wd = wd_map.get((u.id, d))
      if wd and wd.status == "open":
        open_days += 1

      r = calc_day_from_logs(day_logs)
      if r["ok"]:
        ok_days += 1
        total_gross += r["gross_sec"]
        total_break += r["break_sec"]
        total_net += r["net_sec"]
      else:
        inconsistent_day += 1

    rows.append({
      "user": u.name,
      "work_days_with_logs": work_days,
      "ok_days": ok_days,
      "inconsistent_days": inconsistent_day,
      "open_days": open_days,
      "total_net": sec_to_hm(total_net),
      "total_break": sec_to_hm(total_break),
      "total_gross": sec_to_hm(total_gross),
    })

  return {
    "month": month,
    "rows": rows,
  }

@app.get("/admin/user-detail")
def admin_user_detail(
  month: str,
  user: str,
  admin: User = Depends(require_admin),
  db: Session = Depends(get_db),
):
  start, end = month_range(month)

  u = db.query(User).filter(User.name == user, User.is_active == True).one_or_none()
  if not u:
    return {"error": "対象のユーザーが見つかりません"}
  
  logs = (
    db.query(AttendanceLog)
    .filter(AttendanceLog.user_id == u.id, AttendanceLog.ts >= start, AttendanceLog.ts < end)
    .order_by(AttendanceLog.ts.asc())
    .all()
  )

  day_map = group_logs_by_workday_start(logs)
  
  details = []
  for d in sorted(day_map.keys()):
    r = calc_day_from_logs(day_map[d])
    details.append({
      "date": str(d),
      "ok": r["ok"],
      "error": r["error"],
      "start": r["start"].strftime("%H:%M:%S") if r["start"] else None,
      "end": r["end"].strftime("%H:%M:%S") if r["end"] else None,
      "gross": sec_to_hm(r["gross_sec"]),
      "break": sec_to_hm(r["break_sec"]),
      "net": sec_to_hm(r["net_sec"]),
      "action": [
        {"action":l.action, "time": l.ts.strftime("%H:%M:%S"), "lat": l.lat, "lon": l.lon,}
        for l in sorted(day_map[d], key=lambda x: x.ts)
      ],
    })

  return {
    "month": month,
    "user": u.name,
    "details": details,
  }

@app.get("/admin/user-detail.csv")
def admin_user_detail_csv(
  month: str,
  user: str,
  admin: User = Depends(require_admin),
  db: Session = Depends(get_db),
):
  data = admin_user_detail(month=month, user=user, admin=admin, db=db)

  if data.get("error"):
    return data
  
  output = io.StringIO()
  writer = csv.writer(output)

  writer.writerow([
    "month",
    "user",
    "date",
    "ok",
    "error",
    "start",
    "end",
    "gross",
    "break",
    "net",
    "actions",
    "last_lat",
    "last_lon",
  ])

  for d in data["details"]:
    actions = ""
    if d.get("action"):
      actions = " / ".join([f'{a["action"]} {a["time"]} {a["lat"]} {a["lon"]}' for a in d["action"]])

    writer.writerow([
      data["month"],
      data["user"],
      d.get("date"),
      "1" if d.get("ok") else "0",
      d.get("error") or "",
      d.get("start") or "",
      d.get("end") or "",
      d.get("gross") or "",
      d.get("break") or "",
      d.get("net") or "",
      actions,
    ])

  content = output.getvalue().encode("utf-8-sig")
  filename = f"user-detail_{month}_{user}.csv"
  filename_star = quote(filename)
  filename_ascii = filename.encode("ascii", "ignore").decode("ascii") or "user-detail.csv"

  return StreamingResponse(
    io.BytesIO(content),
    media_type="text/csv; charset=utf-8",
    headers={"Content-Disposition": f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{filename_star}'},
  )

@app.get("/admin/month-summary.csv")
def admin_month_summary_csv(
  month: str,
  admin: User = Depends(require_admin),
  db: Session = Depends(get_db)
):
  data = admin_month_summary(month=month, admin=admin, db=db)

  output = io.StringIO()
  writer = csv.writer(output)

  writer.writerow([
    "month",
    "user",
    "work_days_with_logs",
    "ok_days",
    "inconsistent_days",
    "open_days",
    "total_net",
    "total_break",
    "total_gross",
  ])

  for row in data["rows"]:
    writer.writerow([
      data["month"],
      row["user"],
      row["work_days_with_logs"],
      row["ok_days"],
      row["inconsistent_days"],
      row["open_days"],
      row["total_net"],
      row["total_break"],
      row["total_gross"],      
    ])

  content = output.getvalue().encode("utf-8-sig")
  filename = f"month-summary_{month}.csv"
  filename_star = quote(filename)
  filename_ascii = filename.encode("ascii", "ignore").decode("ascii") or "month-summary.csv"

  return StreamingResponse(
    io.BytesIO(content),
    media_type="text/csv; charset=utf-8",
    headers={"Content-Disposition": f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{filename_star}'},
  )

@app.get("/admin/raw-logs.csv")
def admin_raw_logs_csv(
  month: str,
  user: str | None = None,
  admin: User = Depends(require_admin),
  db: Session = Depends(get_db),
):
  start, end = month_range(month)

  q = (
    db.query(AttendanceLog, User)
    .join(User, User.id == AttendanceLog.user_id)
    .filter(AttendanceLog.ts >= start, AttendanceLog.ts < end)
    .order_by(User.id.asc(), AttendanceLog.ts.asc())
  )

  if user:
    u = db.query(User).filter(User.name == user, User.is_active == True).one_or_none()
    if not u:
      raise HTTPException(status_code=404, detail="対象のユーザーが見つかりません")
    q = q.filter(AttendanceLog.user_id == u.id)
  
  rows = q.all()

  output = io.StringIO()
  writer = csv.writer(output)

  writer.writerow([
    "month",
    "user",
    "ts",
    "action",
    "lat",
    "lon",
    "source",
  ])

  for log, u in rows:
    writer.writerow([
      month,
      u.name,
      log.ts.strftime("%Y-%m-%d %H:%M:%S") if log.ts else "",
      log.action or "",
      "" if log.lat is None else log.lat,
      "" if log.lon is None else log.lon,
      "" if getattr(log, "souce", None) is None else log.source,
    ])

  content = output.getvalue().encode("utf-8-sig")
  filename =  f"raw-logs_{month}{('_' + user) if user else ''}.csv"
  filename_star = quote(filename)
  filename_ascii = filename.encode("ascii", "ignore").decode("ascii") or "raw-logs.csv"

  return StreamingResponse(
    io.BytesIO(content),
    media_type="text/csv; charset=utf-8",
    headers={"Content-Disposition": f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{filename_star}'},
  )
