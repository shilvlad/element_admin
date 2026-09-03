import secrets
from cryptography.fernet import Fernet
from .config import settings

fernet = Fernet(settings.password_encryption_key.encode())

def encrypt_secret(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()

def decrypt_secret(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()

def generate_password(length=24):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
