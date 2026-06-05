import os
from db import SessionLocal, init_db, User
from security import hash_pin

def seed_users(db):
    def upsert_user(name: str, pin: str, role: str = "user"):
        u = db.query(User).filter(User.name == name).one_or_none()
        if u:
            u.pin_hash = hash_pin(pin)
            u.role = role
            u.is_active = True
        else:
            u = User(name=name, pin_hash=hash_pin(pin), role=role, is_active=True)
            db.add(u)

    admin_name = os.environ.get("SEED_ADMIN_NAME")
    admin_pin = os.environ.get("SEED_ADMIN_PIN", "0000")
    if admin_name:
        upsert_user(admin_name, admin_pin, role="admin")

    db.commit()
    print("seed ok")


def main():
    init_db()
    db = SessionLocal()
    try:
        seed_users(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()