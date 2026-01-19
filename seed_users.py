from db import SessionLocal, init_db, User
from security import hash_pin

def main():
  init_db()
  db = SessionLocal()

  def upsert_user(name: str, pin: str, role: str = "user"):
    u = db.query(User).filter(User.name == name).one_or_none()
    if u:
      u.pin_hash = hash_pin(pin)
      u.role = role
      u.is_active = True
    else:
      u = User(name=name, pin_hash=hash_pin(pin), role=role, is_active=True)
      db.add(u)

  upsert_user("瀬良 仁", "1234", role="admin")
  upsert_user("瀬良 学", "0000", role="user")
  upsert_user("瀬良 虹々", "1111", role="user")
  upsert_user("瀬良 咲愛菜", "2222", role="user")
  
  db.commit()
  db.close()
  print("seed ok")


if __name__ == "__main__":
  main()
