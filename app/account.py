"""
Account management web pages for Curatio v2.

Provides HTML pages for registration, login, 2FA setup, and app pairing.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/account", tags=["account"])

_COMMON_STYLES = """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a; color: #e0e0e0;
            min-height: 100vh; display: flex; align-items: center; justify-content: center;
        }
        .container {
            background: #1a1a2e; border-radius: 16px; padding: 40px;
            max-width: 440px; width: 100%; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
        }
        h1 { font-size: 1.5rem; margin-bottom: 8px; color: #fff; }
        p.subtitle { color: #888; margin-bottom: 24px; font-size: 0.9rem; }
        label { display: block; font-size: 0.85rem; color: #aaa; margin-bottom: 4px; margin-top: 16px; }
        input[type="text"], input[type="email"], input[type="password"] {
            width: 100%; padding: 12px; border: 1px solid #333; border-radius: 8px;
            background: #0d0d1a; color: #fff; font-size: 1rem; outline: none;
        }
        input:focus { border-color: #6c63ff; }
        button {
            width: 100%; padding: 14px; border: none; border-radius: 8px;
            background: #6c63ff; color: #fff; font-size: 1rem; font-weight: 600;
            cursor: pointer; margin-top: 24px; transition: background 0.2s;
        }
        button:hover { background: #5a52d5; }
        button:disabled { background: #444; cursor: not-allowed; }
        .error { color: #ff6b6b; font-size: 0.85rem; margin-top: 8px; display: none; }
        .success { color: #51cf66; font-size: 0.85rem; margin-top: 8px; display: none; }
        .link { color: #6c63ff; text-decoration: none; }
        .link:hover { text-decoration: underline; }
        .footer { text-align: center; margin-top: 20px; font-size: 0.85rem; color: #666; }
        .qr-container { text-align: center; margin: 20px 0; }
        .qr-container img { border-radius: 8px; }
        .code-display {
            font-family: monospace; font-size: 2rem; letter-spacing: 0.5em;
            text-align: center; padding: 16px; background: #0d0d1a;
            border-radius: 8px; border: 1px solid #333; margin: 16px 0;
            color: #6c63ff; font-weight: bold;
        }
    </style>
"""


@router.get("/register", response_class=HTMLResponse)
async def register_page():
    """Registration page."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Create Account - Curatio</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <h1>Create Account</h1>
        <p class="subtitle">Enter your invite code to get started.</p>

        <form id="registerForm">
            <label for="invite">Invite Code</label>
            <input type="text" id="invite" name="invite" required autocomplete="off">

            <label for="name">Display Name (optional)</label>
            <input type="text" id="name" name="name" autocomplete="name">

            <label for="email">Email</label>
            <input type="email" id="email" name="email" required autocomplete="email">

            <label for="password">Password (min 8 characters)</label>
            <input type="password" id="password" name="password" required minlength="8" autocomplete="new-password">

            <div class="error" id="error"></div>
            <div class="success" id="success"></div>

            <button type="submit" id="submitBtn">Create Account</button>
        </form>

        <div class="footer">
            Already have an account? <a href="/account/login" class="link">Log in</a>
        </div>
    </div>

    <script>
        document.getElementById('registerForm').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const btn = document.getElementById('submitBtn');
            const error = document.getElementById('error');
            const success = document.getElementById('success');
            error.style.display = 'none';
            success.style.display = 'none';
            btn.disabled = true;
            btn.textContent = 'Creating...';

            try {{
                const resp = await fetch('/auth/register', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        invite: document.getElementById('invite').value,
                        email: document.getElementById('email').value,
                        password: document.getElementById('password').value,
                        name: document.getElementById('name').value,
                    }})
                }});
                const data = await resp.json();
                if (!resp.ok) {{
                    error.textContent = data.detail || 'Registration failed';
                    error.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Create Account';
                    return;
                }}
                success.textContent = 'Account created! Redirecting to login...';
                success.style.display = 'block';
                setTimeout(() => window.location.href = '/account/login', 1500);
            }} catch (err) {{
                error.textContent = 'Network error. Please try again.';
                error.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Create Account';
            }}
        }});
    </script>
</body>
</html>"""
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log In - Curatio</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <h1>Log In</h1>
        <p class="subtitle">Sign in to manage your account and pair your app.</p>

        <form id="loginForm">
            <label for="email">Email</label>
            <input type="email" id="email" name="email" required autocomplete="email">

            <label for="password">Password</label>
            <input type="password" id="password" name="password" required autocomplete="current-password">

            <div id="totpGroup" style="display:none;">
                <label for="totp_code">2FA Code</label>
                <input type="text" id="totp_code" name="totp_code" autocomplete="one-time-code"
                       pattern="[0-9]{{6}}" maxlength="6" placeholder="6-digit code">
            </div>

            <div class="error" id="error"></div>

            <button type="submit" id="submitBtn">Log In</button>
        </form>

        <div class="footer">
            Need an account? <a href="/account/register" class="link">Register</a>
        </div>
    </div>

    <script>
        const form = document.getElementById('loginForm');
        const totpGroup = document.getElementById('totpGroup');
        let show2fa = false;

        form.addEventListener('submit', async (e) => {{
            e.preventDefault();
            const btn = document.getElementById('submitBtn');
            const error = document.getElementById('error');
            error.style.display = 'none';
            btn.disabled = true;
            btn.textContent = 'Logging in...';

            try {{
                const resp = await fetch('/auth/login', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        email: document.getElementById('email').value,
                        password: document.getElementById('password').value,
                        totp_code: document.getElementById('totp_code').value || '',
                    }})
                }});
                const data = await resp.json();
                if (!resp.ok) {{
                    if (data.detail === '2FA code required') {{
                        totpGroup.style.display = 'block';
                        document.getElementById('totp_code').focus();
                        error.textContent = 'Enter your 2FA code from your authenticator app.';
                        error.style.display = 'block';
                        btn.disabled = false;
                        btn.textContent = 'Log In';
                        return;
                    }}
                    error.textContent = data.detail || 'Login failed';
                    error.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Log In';
                    return;
                }}
                // Login success — check if 2FA needs setup
                if (!data.totp_enabled) {{
                    window.location.href = '/account/setup-2fa';
                }} else {{
                    window.location.href = '/account/pair';
                }}
            }} catch (err) {{
                error.textContent = 'Network error. Please try again.';
                error.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Log In';
            }}
        }});
    </script>
</body>
</html>"""
    )


@router.get("/setup-2fa", response_class=HTMLResponse)
async def setup_2fa_page():
    """2FA setup page — shows QR code for authenticator app."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Set Up 2FA - Curatio</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <h1>Set Up Two-Factor Authentication</h1>
        <p class="subtitle">Scan this QR code with your authenticator app (Google Authenticator, Authy, etc).</p>

        <div class="qr-container" id="qrContainer">
            <p style="color: #888;">Loading...</p>
        </div>

        <div id="secretDisplay" style="display:none;">
            <label>Manual entry key:</label>
            <div class="code-display" id="secretCode" style="font-size: 0.9rem; letter-spacing: 0.2em;"></div>
        </div>

        <form id="verifyForm" style="display:none;">
            <label for="code">Enter the 6-digit code from your app to verify</label>
            <input type="text" id="code" name="code" pattern="[0-9]{{6}}" maxlength="6"
                   placeholder="000000" required autocomplete="one-time-code">

            <div class="error" id="error"></div>
            <div class="success" id="success"></div>

            <button type="submit" id="submitBtn">Verify & Enable 2FA</button>
        </form>
    </div>

    <script>
        async function setup() {{
            try {{
                const resp = await fetch('/auth/setup-2fa', {{ method: 'POST' }});
                if (!resp.ok) {{
                    if (resp.status === 401) {{ window.location.href = '/account/login'; return; }}
                    const data = await resp.json();
                    document.getElementById('qrContainer').innerHTML =
                        '<p style="color:#ff6b6b;">' + (data.detail || 'Failed to set up 2FA') + '</p>';
                    return;
                }}
                const data = await resp.json();
                document.getElementById('qrContainer').innerHTML =
                    '<img src="' + data.qr_data_url + '" alt="QR Code" width="200" height="200">';
                document.getElementById('secretCode').textContent = data.secret;
                document.getElementById('secretDisplay').style.display = 'block';
                document.getElementById('verifyForm').style.display = 'block';
            }} catch (err) {{
                document.getElementById('qrContainer').innerHTML =
                    '<p style="color:#ff6b6b;">Network error. Please refresh.</p>';
            }}
        }}
        setup();

        document.getElementById('verifyForm').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const btn = document.getElementById('submitBtn');
            const error = document.getElementById('error');
            const success = document.getElementById('success');
            error.style.display = 'none';
            btn.disabled = true;

            try {{
                const resp = await fetch('/auth/confirm-2fa', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{ code: document.getElementById('code').value }})
                }});
                const data = await resp.json();
                if (!resp.ok) {{
                    error.textContent = data.detail || 'Verification failed';
                    error.style.display = 'block';
                    btn.disabled = false;
                    return;
                }}
                success.textContent = '2FA enabled! Redirecting to app pairing...';
                success.style.display = 'block';
                setTimeout(() => window.location.href = '/account/pair', 1500);
            }} catch (err) {{
                error.textContent = 'Network error.';
                error.style.display = 'block';
                btn.disabled = false;
            }}
        }});
    </script>
</body>
</html>"""
    )


@router.get("/pair", response_class=HTMLResponse)
async def pair_page():
    """App pairing page — shows QR code and short code for the custom Stremio app."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pair a TV Device - Curatio</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <h1>Pair a TV Device</h1>
        <p class="subtitle">Enter the code shown on your Apple TV or other device.</p>

        <div style="display:flex; gap:8px; justify-content:center; align-items:center; flex-wrap:wrap;">
            <input type="text" id="deviceCodeInput" maxlength="6" placeholder="ABC123"
                   style="font-family:monospace; font-size:1.4rem; letter-spacing:0.3em;
                          text-align:center; width:180px; padding:10px; border-radius:8px;
                          border:1px solid #444; background:#1a1a2e; color:#fff;
                          text-transform:uppercase;">
            <button id="deviceClaimBtn" onclick="claimDevice()">Pair</button>
        </div>
        <div class="error" id="deviceError"></div>
        <div id="deviceSuccess" style="display:none; color:#4caf50; text-align:center; margin-top:8px;"></div>

        <div class="footer" style="margin-top: 24px;">
            <a href="/auth/logout" class="link" onclick="fetch('/auth/logout',{{method:'POST'}})">Log out</a>
        </div>
    </div>

    <script>
        async function claimDevice() {{
            const input = document.getElementById('deviceCodeInput');
            const error = document.getElementById('deviceError');
            const success = document.getElementById('deviceSuccess');
            const btn = document.getElementById('deviceClaimBtn');
            error.style.display = 'none';
            success.style.display = 'none';

            const code = input.value.trim().toUpperCase();
            if (!code || code.length < 4) {{
                error.textContent = 'Please enter the code shown on your device.';
                error.style.display = 'block';
                return;
            }}

            btn.disabled = true;
            btn.textContent = 'Pairing...';

            try {{
                const resp = await fetch('/auth/device/claim', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ short_code: code }})
                }});
                if (!resp.ok) {{
                    if (resp.status === 401) {{ window.location.href = '/account/login'; return; }}
                    const data = await resp.json();
                    error.textContent = data.detail || 'Invalid or expired code.';
                    error.style.display = 'block';
                    return;
                }}
                success.textContent = 'Device paired successfully!';
                success.style.display = 'block';
                input.value = '';
            }} catch (err) {{
                error.textContent = 'Network error. Please try again.';
                error.style.display = 'block';
            }} finally {{
                btn.disabled = false;
                btn.textContent = 'Pair';
            }}
        }}

        document.getElementById('deviceCodeInput').addEventListener('keydown', function(e) {{
            if (e.key === 'Enter') claimDevice();
        }});

    </script>
</body>
</html>"""
    )


@router.get("/activate", response_class=HTMLResponse)
async def activate_page():
    """Device activation page — enter the code shown on your TV to pair it."""
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Activate Device - Curatio</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <h1>Activate Your Device</h1>
        <p class="subtitle">Enter the code shown on your Apple TV to link it to your account.</p>

        <div style="display:flex; gap:8px; justify-content:center; align-items:center; flex-wrap:wrap; margin-top:16px;">
            <input type="text" id="deviceCodeInput" maxlength="6" placeholder="ABC123" autofocus
                   style="font-family:monospace; font-size:1.6rem; letter-spacing:0.3em;
                          text-align:center; width:200px; padding:12px; border-radius:8px;
                          border:1px solid #444; background:#1a1a2e; color:#fff;
                          text-transform:uppercase;">
            <button id="deviceClaimBtn" onclick="claimDevice()">Activate</button>
        </div>
        <div class="error" id="deviceError"></div>
        <div id="deviceSuccess" style="display:none; color:#4caf50; text-align:center; margin-top:12px; font-size:1.1rem;"></div>

        <div class="footer" style="margin-top: 32px;">
            <a href="/account" class="link">Back to Account</a>
        </div>
    </div>

    <script>
        async function claimDevice() {{
            const input = document.getElementById('deviceCodeInput');
            const error = document.getElementById('deviceError');
            const success = document.getElementById('deviceSuccess');
            const btn = document.getElementById('deviceClaimBtn');
            error.style.display = 'none';
            success.style.display = 'none';

            const code = input.value.trim().toUpperCase();
            if (!code || code.length < 4) {{
                error.textContent = 'Please enter the code shown on your device.';
                error.style.display = 'block';
                return;
            }}

            btn.disabled = true;
            btn.textContent = 'Activating...';

            try {{
                const resp = await fetch('/auth/device/claim', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ short_code: code }})
                }});
                if (!resp.ok) {{
                    if (resp.status === 401) {{ window.location.href = '/account/login'; return; }}
                    const data = await resp.json();
                    error.textContent = data.detail || 'Invalid or expired code. Please try again.';
                    error.style.display = 'block';
                    return;
                }}
                success.textContent = 'Device activated successfully! You can now use your TV app.';
                success.style.display = 'block';
                input.value = '';
            }} catch (err) {{
                error.textContent = 'Network error. Please try again.';
                error.style.display = 'block';
            }} finally {{
                btn.disabled = false;
                btn.textContent = 'Activate';
            }}
        }}

        document.getElementById('deviceCodeInput').addEventListener('keydown', function(e) {{
            if (e.key === 'Enter') claimDevice();
        }});
    </script>
</body>
</html>"""
    )
