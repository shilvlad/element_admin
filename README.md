# Synapse User Manager v2

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
