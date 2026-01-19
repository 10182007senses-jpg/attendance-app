import bcrypt


def hash_pin(pin: str) -> str:
  pin_bytes = pin.encode("utf-8")
  hashed = bcrypt.hashpw(pin_bytes, bcrypt.gensalt())
  return hashed.decode("utf-8")

def verify_pin(pin: str, pin_hash: str) -> bool:
  return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))
