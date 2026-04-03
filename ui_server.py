import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

load_dotenv()

USERNAME       = os.environ.get('UI_USERNAME', 'admin')
PASSWORD       = os.environ.get('UI_PASSWORD', 'changeme')
SESSION_SECRET = os.environ.get('UI_SESSION_SECRET', 'change-me')
SESSION_MAX_AGE = 86400 * 30  # 30 days

signer = URLSafeTimedSerializer(SESSION_SECRET)

COOKIE = 'tmuxer_session'
PUBLIC_PATHS = {'/login'}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        token = request.cookies.get(COOKIE)
        try:
            signer.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired, Exception):
            return RedirectResponse('/login', status_code=302)
        return await call_next(request)


app = FastAPI()
app.add_middleware(AuthMiddleware)


@app.get('/login', response_class=HTMLResponse)
async def login_page(error: str = ''):
    return _login_html(error=bool(error))


@app.post('/login')
async def login(username: str = Form(...), password: str = Form(...)):
    if username == USERNAME and password == PASSWORD:
        token = signer.dumps(username)
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, samesite='lax', max_age=SESSION_MAX_AGE)
        return response
    return _login_html(error=True)


@app.get('/logout')
async def logout():
    response = RedirectResponse('/login', status_code=302)
    response.delete_cookie(COOKIE)
    return response


app.mount('/', StaticFiles(directory='sample_ui', html=True), name='ui')


def _login_html(error: bool = False) -> HTMLResponse:
    error_html = '<p class="error">Invalid username or password.</p>' if error else ''
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>tmuxer · login</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:      #383d48;
      --surface: #42474f;
      --border:  #575e6b;
      --accent:  #58a6ff;
      --text:    #e8ecf0;
      --muted:   #adb4bc;
      --danger:  #f85149;
    }}
    html, body {{
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 13px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 32px 36px;
      width: 340px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    h1 {{ font-size: 16px; font-weight: 600; }}
    label {{ font-size: 11px; color: var(--muted); font-weight: 500; display: block; margin-bottom: 4px; }}
    input {{
      width: 100%;
      padding: 7px 10px;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      font-size: 12px;
      outline: none;
      transition: border-color .15s;
    }}
    input:focus {{ border-color: var(--accent); }}
    button {{
      padding: 8px;
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity .15s;
    }}
    button:hover {{ opacity: .85; }}
    .error {{ color: var(--danger); font-size: 11px; }}
  </style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <h1>tmuxer</h1>
    {error_html}
    <div>
      <label for="username">Username</label>
      <input id="username" name="username" type="text" autocomplete="username" autofocus />
    </div>
    <div>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" />
    </div>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>"""
    return HTMLResponse(html)


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('UI_PORT', 5679))
    uvicorn.run(app, host='0.0.0.0', port=port)
