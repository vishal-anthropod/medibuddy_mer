# App Login Protection

The Flask app has an app-level login gate. Users cannot access pages or APIs unless they are authenticated.

## Vercel Environment Variables

Set these in Vercel Project Settings -> Environment Variables:

| Variable | Required | Example |
| --- | --- | --- |
| `APP_LOGIN_USERNAME` | Optional | `admin` |
| `APP_LOGIN_PASSWORD` | Yes | use a strong password |
| `APP_SECRET_KEY` | Yes | a long random string |

If `APP_LOGIN_PASSWORD` is missing, the app stays locked and login returns a configuration error.

## Routes

| Route | Behavior |
| --- | --- |
| `/login` | Login form. |
| `/logout` | Clears session and returns to login. |
| `/static/...` | Allowed without login so CSS/assets can load. |
| `/api/...` | Returns `401` JSON when not logged in. |
| All other routes | Redirect to `/login` when not logged in. |

## Local Development

Run locally with:

```bash
APP_LOGIN_USERNAME=admin APP_LOGIN_PASSWORD='your-password' APP_SECRET_KEY='local-dev-secret' python3 app.py
```

To temporarily disable login locally:

```bash
APP_LOGIN_ENABLED=false python3 app.py
```
