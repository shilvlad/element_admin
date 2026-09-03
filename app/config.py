from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Synapse User Manager"
    secret_key: str = "change-me"
    database_url: str = "sqlite:///./data/synapse-user-manager.db"
    matrix_homeserver: str = "https://chat.iteko.su"
    matrix_server_name: str = "chat.iteko.su"
    matrix_admin_token: str = ""
    global_room_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    admin_email: str = ""
    admin_username: str = "admin"
    admin_password: str = "change-me"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
