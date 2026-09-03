# Synapse User Manager

Внутренняя панель управления пользователями Synapse.

## Возможности
- создание заявки;
- модерация approve/reject;
- SMTP-уведомление администратора;
- создание через Synapse Admin API;
- invite в #Global;
- поиск пользователей;
- сброс пароля;
- activate/deactivate;
- audit log;
- Docker Compose.

## Запуск

```bash
cp .env.example .env
# заполнить .env
mkdir -p data
docker compose up -d --build
```

Панель: `http://127.0.0.1:8080` локально или через Nginx.

## Важно
Перед production обязательно заменить локальную авторизацию на LDAP/AD/SSO, добавить CSRF/rate limiting, хранить секреты в Vault/KMS и рассмотреть PostgreSQL вместо SQLite.

`MATRIX_ADMIN_TOKEN` должен принадлежать отдельной сервисной учётной записи и не попадать в git.
`GLOBAL_ROOM_ID` должен содержать реальный Matrix Room ID комнаты `#Global:chat.iteko.su`.
