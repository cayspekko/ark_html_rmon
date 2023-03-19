from passlib.context import CryptContext
from tinydb import TinyDB, Query

def change_password(username, psw):
    User = Query()
    users = TinyDB('db.json')

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    new_pw = pwd_context.hash(psw)

    users.upsert({'username': username, 'password': new_pw}, User.username == username)

if __name__ == "__main__":
    import sys
    username = sys.argv[1]
    password = sys.argv[2]

    change_password(username, password)