"""認証まわりの共通ヘルパー（Cookie 認証とモバイル Bearer 認証の両方で使う）。"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

import bcrypt

# ---- PIN -----------------------------------------------------------------

PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 64  # bcrypt は 72 バイトまでしか扱えないので、余裕を持って制限する


def hash_pin(pin: str) -> str:
  pin_bytes = pin.encode("utf-8")
  hashed = bcrypt.hashpw(pin_bytes, bcrypt.gensalt())
  return hashed.decode("utf-8")


def verify_pin(pin: str, pin_hash: str) -> bool:
  if not pin or len(pin) > PIN_MAX_LENGTH:
    return False
  try:
    return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))
  except ValueError:
    return False


# ユーザーが存在しないときも同じ時間だけ bcrypt を回すためのダミーハッシュ
_DUMMY_PIN_HASH = hash_pin(secrets.token_hex(16))


def verify_pin_or_dummy(pin: str, pin_hash: str | None) -> bool:
  """pin_hash が None（ユーザーなし）でもダミー検証を行い、応答時間をそろえる。"""
  if pin_hash is None:
    verify_pin(pin, _DUMMY_PIN_HASH)
    return False
  return verify_pin(pin, pin_hash)


def validate_new_pin(pin: str) -> str | None:
  """新しく設定する PIN の強度チェック。問題があればエラーメッセージを返す。"""
  if len(pin) < PIN_MIN_LENGTH:
    return f"PINは{PIN_MIN_LENGTH}文字以上にしてください"
  if len(pin) > PIN_MAX_LENGTH:
    return f"PINは{PIN_MAX_LENGTH}文字以内にしてください"
  if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
    return "推測されやすいPINは使用できません"
  return None


# ---- セッショントークン ---------------------------------------------------


def new_session_token() -> str:
  return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
  """DB にはトークンそのものではなく SHA-256 ハッシュ（64文字）を保存する。"""
  return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cookie_secure() -> bool:
  """ローカル開発（COOKIE_INSECURE=1）以外では常に Secure 属性を付ける。"""
  return os.getenv("COOKIE_INSECURE") != "1"


# ---- ログイン試行制限 -----------------------------------------------------
# プロセス内メモリで管理する。複数ワーカーで動かす場合は Redis 等に置き換えること。

LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
LOGIN_LOCKOUT = timedelta(minutes=15)
IP_MAX_FAILURES = 30

_login_lock = threading.Lock()
_failures_by_login: dict[str, deque[datetime]] = defaultdict(deque)
_failures_by_ip: dict[str, deque[datetime]] = defaultdict(deque)
_locked_until: dict[str, datetime] = {}


def _prune(q: deque[datetime], now: datetime) -> None:
  while q and now - q[0] > LOGIN_FAILURE_WINDOW:
    q.popleft()


def login_blocked(login_id: str, ip: str, now: datetime) -> bool:
  key = login_id.strip().lower()
  with _login_lock:
    until = _locked_until.get(key)
    if until and until > now:
      return True
    ip_q = _failures_by_ip[ip]
    _prune(ip_q, now)
    return len(ip_q) >= IP_MAX_FAILURES


def record_login_failure(login_id: str, ip: str, now: datetime) -> None:
  key = login_id.strip().lower()
  with _login_lock:
    q = _failures_by_login[key]
    _prune(q, now)
    q.append(now)
    _failures_by_ip[ip].append(now)
    if len(q) >= LOGIN_MAX_FAILURES:
      _locked_until[key] = now + LOGIN_LOCKOUT
      q.clear()


def clear_login_failures(login_id: str) -> None:
  key = login_id.strip().lower()
  with _login_lock:
    _failures_by_login.pop(key, None)
    _locked_until.pop(key, None)


LOGIN_BLOCKED_MESSAGE = "ログイン試行回数が上限に達しました。しばらくしてから再度お試しください"
