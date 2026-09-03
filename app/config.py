from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Synapse User Manager"
    app_base_url: str = "http://localhost:8080"
    secret_key: str
    database_url: str = "sqlite:///./data/synapse-user-manager.db"
    password_encryption_key: str
    matrix_homeserver: str
    matrix_server_name: str
    matrix_admin_token: str
    global_room_id: str
    matrix_timeout: float = 10.0
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str
    smtp_password: str
    smtp_starttls: bool = True
    admin_email: str
    admin_username: str
    admin_password: str
    enable_diagnostics: bool = True
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
