from sqlmodel import Session, select
from backend.database import engine
from backend.models import User
from backend.core.security import get_password_hash

def reset_password():
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == "admin")).first()
        if not user:
            print("User 'admin' not found.")
            return

        new_password = "admin" # Setting to simple 'admin' for ease of testing based on potential user intent
        user.hashed_password = get_password_hash(new_password)
        session.add(user)
        session.commit()
        print(f"Password for user '{user.username}' has been reset to '{new_password}'.")

if __name__ == "__main__":
    reset_password()
