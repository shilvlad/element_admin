# Synapse User Manager v2

### Версия и сборка

Справа сверху — версия панели и коммит; подробности доступны на странице «О сборке». При автоматическом деплое коммит и дата записываются в образ. При ручной сборке:

```bash
docker compose build --build-arg BUILD_COMMIT="$(git rev-parse HEAD)" --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
docker compose up -d
```

Без аргументов сборки коммит и дата отмечены как неизвестные. Версия и список изменений задаются в `app/build.py`.


Production-oriented FastAPI service for managing Synapse users.

## Main improvements
- encrypted temporary registration passwords with Fernet; password is removed after successful approval;
- dedicated APP_BASE_URL for email links;
- authenticated diagnostics page `/diagnostics`;
- explicit Admin API connectivity/token test;
- correct HTTP error handling (no treating 403 as success);
- URL-encoding of Matrix user/room IDs;
- health endpoint `/health`;
- secure session cookie when APP_BASE_URL uses HTTPS.

## Important
The `MATRIX_ADMIN_TOKEN` must be a valid Synapse admin access token. Do not expose it to the browser or put it into logs.

The current room invite implementation uses the token as a Matrix client token. Depending on Synapse/token type, a dedicated client-capable service account may be required for invites. The diagnostics page deliberately reports this separately; it does not claim that room membership works merely because Admin API works.

For production, use PostgreSQL, HTTPS, CSRF protection, rate limiting, SSO/LDAP/AD, secrets management (Vault/KMS), migrations, and a dedicated service account with least privilege where supported.

## Generate secrets

```bash
openssl rand -hex 32
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```
Deployment test: GitHub Actions

## Регистрация с паролем пользователя

На `/request` заявитель выбирает пароль (8–128 символов) и повторяет его.
Пароль временно хранится с шифрованием Fernet; после создания учётной записи
или отклонения заявки удаляется. Администратор не получает пароль.
После одобрения пользователь входит с выбранным паролем; статус уточняет
у администратора. Новые уведомления не добавлены; существующее письмо
администратору о новой заявке сохранено.

Логин проверяется в Synapse при подаче и непосредственно перед созданием.
Таблица `registration_reservations` создаётся автоматически при запуске и
защищает от параллельных заявок на один логин. Существующие заявки также
проверяются; дубликат старой заявки следует отклонить. После отклонения
резерв освобождается. Миграция существующих полей не требуется.

Приглашение в Global выполняется после сохранения созданной учётной записи.
Если оно не удалось, заявка остаётся `APPROVED`: кнопка повтора отправляет
только приглашение, не меняя пароль и права пользователя.

### Незавершённые операции и старые заявки

Для `CREATING`, `CREATED`, `ERROR` и `RECOVERING` доступна кнопка сверки с Synapse.
Недавние операции защищены паузой не менее двух минут. Найденную запись нужно
явно подтвердить; сверка не меняет пароль и права. Если запись не найдена,
повторное создание разрешается только при сохранённом пароле заявки.
Старые заявки с автоматически созданным паролем требуют его сброса после одобрения.

### Панель администратора

Карточка пользователя показывает устройства, подключения, комнаты и последние
100 действий панели. На главной — доступность Admin API, пользователи,
последняя активность, заявки и ошибки за сутки. В комнатах доступны участники,
приглашения и постраничный просмотр известных медиа с размерами файлов.
Зашифрованные вложения могут отсутствовать в Media API; объём на странице
не является полным размером хранилища комнаты.

В новых заявках требуется email: он сохраняется в профиле Synapse при одобрении.
После сброса или активации новый пароль отправляется на первый корректный email
из профиля. Если адреса нет либо SMTP недоступен, пароль показывается администратору
для ручной передачи. Исходные пароли заявок по почте не отправляются.

Все POST-формы, включая выход, защищены CSRF. По умолчанию доступно 10 попыток
входа за минуту (по IP и логину) и 5 заявок за час на IP; значения задаются
`LOGIN_ATTEMPTS` и `REGISTRATION_ATTEMPTS`. Счётчики общие для процессов и хранятся
в БД, новые таблицы создаются при запуске. За nginx укажите в
`FORWARDED_ALLOW_IPS` только реальные доверенные адреса прокси, включая адрес
Docker-шлюза при необходимости; иначе лимит будет общим для всех за прокси.
В nginx передавайте `X-Forwarded-For $remote_addr`, как в примере.

Synapse Admin API PUT совмещает создание и обновление. Проверка GET и локальная
резервация защищают от конфликтов внутри панели; одновременное создание того же
логина через другие сервисы нужно исключить.


### Локальный SMTP без авторизации

`SMTP_FROM` задаёт адрес отправителя независимо от логина SMTP. Если он не задан,
используется `SMTP_USER` для совместимости. Для доверенного локального Postfix
задайте `SMTP_FROM=matrix@chat.iteko.su`, пустые `SMTP_USER` и `SMTP_PASSWORD`,
`SMTP_PORT=25`, `SMTP_STARTTLS=false`, а в `SMTP_HOST` — доступный из контейнера
адрес хоста. Доступ к этому SMTP должен быть ограничен хостом и контейнером панели.

### Уведомления в Matrix

На странице «Настройки» включите уведомления о заявках и укажите получателей (например, `serg`). Отправитель — Matrix-аккаунт из `MATRIX_ADMIN_TOKEN`, его ID показан на странице. Получателю нужно принять приглашение в приватную комнату. Пароли не отправляются; ошибки доставки видны в аудите и не мешают созданию заявки. Настройки и комнаты сохраняются в БД панели.
