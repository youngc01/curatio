"""
HTML templates for the addon landing / configuration page.
"""

from app.config import settings


def landing_page_html() -> str:
    """Landing page with install options and Trakt connect."""
    base_url = settings.base_url

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Curatio — AI-Curated Cinema for Stremio</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,sans-serif;
  background:#0d0d0d;color:#e5e5e5;min-height:100vh;
  min-height:100dvh;
  display:flex;flex-direction:column;align-items:center;
  -webkit-text-size-adjust:100%;
}}
.hero{{
  width:100%;padding:60px 20px 40px;text-align:center;
  background:linear-gradient(180deg,#1a1a2e 0%,#0d0d0d 100%);
}}
.hero h1{{font-size:2.4rem;font-weight:700;margin-bottom:8px;letter-spacing:2px}}
.hero h1 span{{color:#a855f7}}
.hero p{{color:#999;font-size:1.1rem;max-width:520px;margin:0 auto;line-height:1.5}}
.container{{max-width:680px;width:100%;padding:0 20px}}
.card{{
  background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;
  padding:32px;margin-top:28px;
}}
.card h2{{font-size:1.25rem;margin-bottom:6px}}
.card .subtitle{{color:#888;font-size:.9rem;margin-bottom:20px;line-height:1.4}}
.btn{{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:14px 28px;border-radius:10px;font-size:1rem;font-weight:600;
  cursor:pointer;border:none;text-decoration:none;transition:all .2s;
  width:100%;min-height:48px;
  -webkit-tap-highlight-color:transparent;
}}
.btn:active{{transform:scale(.98);opacity:.9}}
.btn-primary{{background:#a855f7;color:#fff}}
.btn-primary:hover{{background:#7c3aed}}
.btn-secondary{{background:#2a2a2a;color:#e5e5e5;border:1px solid #444}}
.btn-secondary:hover{{background:#333}}
.btn-trakt{{background:#ed1c24;color:#fff}}
.btn-trakt:hover{{background:#c8171e}}
.divider{{
  display:flex;align-items:center;gap:16px;margin:28px 0;color:#555;font-size:.85rem;
  white-space:nowrap;
}}
.divider::before,.divider::after{{content:'';flex:1;border-top:1px solid #333}}
.input-group{{margin-bottom:16px}}
.input-group label{{display:block;font-size:.85rem;color:#999;margin-bottom:6px}}
.input-group input{{
  width:100%;padding:14px;background:#111;border:1px solid #333;
  border-radius:8px;color:#e5e5e5;font-size:16px;outline:none;
  -webkit-appearance:none;appearance:none;
}}
.input-group input:focus{{border-color:#a855f7;box-shadow:0 0 0 3px rgba(168,85,247,.15)}}
.error-msg{{
  color:#f87171;font-size:.85rem;margin-top:8px;display:none;
}}
.features{{
  display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:28px;
  margin-bottom:20px;
}}
.feature{{
  background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:20px;
}}
.feature .icon{{font-size:1.6rem;margin-bottom:8px}}
.feature h3{{font-size:.95rem;margin-bottom:4px}}
.feature p{{color:#888;font-size:.8rem;line-height:1.5}}
.manifest-url{{
  margin-top:16px;padding:12px 14px;background:#111;border:1px solid #333;
  border-radius:8px;font-family:monospace;font-size:.8rem;color:#888;
  word-break:break-all;user-select:all;-webkit-user-select:all;
  overflow-x:auto;
}}
footer{{
  margin-top:auto;padding:32px 20px;text-align:center;
  color:#555;font-size:.8rem;
  padding-bottom:max(32px,env(safe-area-inset-bottom));
}}
footer a{{color:#888;text-decoration:none}}
footer a:hover{{color:#e5e5e5}}
@media(max-width:600px){{
  .hero{{padding:40px 16px 28px}}
  .hero h1{{font-size:1.8rem;letter-spacing:1px}}
  .hero p{{font-size:.95rem}}
  .container{{padding:0 16px}}
  .card{{padding:24px 18px;margin-top:20px;border-radius:10px}}
  .card h2{{font-size:1.15rem}}
  .card .subtitle{{font-size:.85rem}}
  .btn{{padding:14px 20px;font-size:.95rem;border-radius:10px}}
  .divider{{margin:20px 0;font-size:.8rem}}
  .features{{grid-template-columns:1fr;gap:12px;margin-top:20px}}
  .feature{{padding:16px}}
  .feature .icon{{font-size:1.4rem;margin-bottom:6px}}
  .feature h3{{font-size:.9rem}}
  .feature p{{font-size:.8rem}}
  .manifest-url{{font-size:.7rem;padding:10px 12px}}
  footer{{padding:24px 16px}}
}}
@media(max-width:360px){{
  .hero h1{{font-size:1.5rem}}
  .hero p{{font-size:.88rem}}
  .card{{padding:20px 14px}}
  .btn{{padding:12px 16px;font-size:.9rem}}
}}
</style>
</head>
<body>

<div class="hero">
  <h1><span>CURATIO</span></h1>
  <p>AI-curated cinema for Stremio. 40 universal catalogs, 14 personalized rows, and 150k+ tagged titles.</p>
</div>

<div class="container">

  <!-- Invite Code Card -->
  <div class="card">
    <h2>Get Started</h2>
    <p class="subtitle">Enter your invite code to install the addon or connect your Trakt account.</p>

    <div class="input-group">
      <label for="invite-code">Invite Code</label>
      <input type="text" id="invite-code" placeholder="Enter your invite code" required autocomplete="off">
    </div>
    <p class="error-msg" id="error-msg"></p>

    <div style="display:flex;flex-direction:column;gap:12px">
      <button class="btn btn-primary" onclick="verifyAndInstall()">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Install in Stremio
      </button>

      <div class="divider">or personalise with Trakt</div>

      <button class="btn btn-trakt" onclick="startTraktAuth()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 012 2v14a2 2 0 01-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
        Connect with Trakt
      </button>

      <div class="divider">or create an account</div>

      <a href="/account/register" class="btn btn-secondary" style="text-decoration:none;text-align:center">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4-4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>
        Create Account
      </a>
    </div>

    <div class="manifest-url" id="manifest-url" style="display:none"></div>
  </div>

  <!-- Feature Grid -->
  <div class="features">
    <div class="feature">
      <div class="icon">&#127916;</div>
      <h3>54 Catalog Rows</h3>
      <p>40 universal catalogs plus 14 personalized rows — Up Next, Top 10 Today, Because You Watched, and more.</p>
    </div>
    <div class="feature">
      <div class="icon">&#129302;</div>
      <h3>AI-Powered Tagging</h3>
      <p>Every title tagged with semantic labels by AI for precise, nuanced matching.</p>
    </div>
    <div class="feature">
      <div class="icon">&#128202;</div>
      <h3>Trakt Personalization</h3>
      <p>Up Next, Because You Watched, and Recommended For You — all powered by your watch history.</p>
    </div>
    <div class="feature">
      <div class="icon">&#128293;</div>
      <h3>Top 10 &amp; Trending</h3>
      <p>Today's most-watched ranked 1-10, plus what's trending and all-time popular — updated daily.</p>
    </div>
  </div>
</div>

<footer>
  Curatio &middot; Powered by Gemini &amp; TMDB<br>
  <a href="/account/login" style="color:#a855f7">Log in to your account</a>
</footer>

<script>
function getCode() {{
  var code = document.getElementById('invite-code').value.trim();
  var errEl = document.getElementById('error-msg');
  errEl.style.display = 'none';
  if (!code) {{
    errEl.textContent = 'Please enter an invite code.';
    errEl.style.display = 'block';
    return null;
  }}
  return code;
}}

function verifyAndInstall() {{
  var code = getCode();
  if (!code) return;
  var errEl = document.getElementById('error-msg');

  fetch('/auth/verify-invite?invite=' + encodeURIComponent(code)).then(function(resp) {{
    if (resp.ok) {{
      return resp.json().then(function(data) {{
        var token = data.install_token;
        var baseHost = '{base_url.replace("https://", "").replace("http://", "")}';
        var stremioUrl = 'stremio://' + baseHost + '/' + token + '/manifest.json';
        var manifestUrl = '{base_url}/' + token + '/manifest.json';
        document.getElementById('manifest-url').textContent = manifestUrl;
        document.getElementById('manifest-url').style.display = 'block';
        window.location.href = stremioUrl;
      }});
    }} else {{
      return resp.json().then(function(data) {{
        errEl.textContent = data.detail || 'Invalid or expired invite code.';
        errEl.style.display = 'block';
      }});
    }}
  }}).catch(function() {{
    errEl.textContent = 'Something went wrong. Please try again.';
    errEl.style.display = 'block';
  }});
}}

function startTraktAuth() {{
  var code = getCode();
  if (!code) return;
  var errEl = document.getElementById('error-msg');

  // Verify invite code first, then redirect to Trakt OAuth
  fetch('/auth/start?invite=' + encodeURIComponent(code), {{
    method: 'GET',
    redirect: 'manual'
  }}).then(function(resp) {{
    if (resp.type === 'opaqueredirect' || resp.status === 307 || resp.status === 302 || resp.status === 303) {{
      window.location.href = '/auth/start?invite=' + encodeURIComponent(code);
    }} else if (resp.status === 403) {{
      errEl.textContent = 'Invalid or expired invite code. Please try again.';
      errEl.style.display = 'block';
    }} else {{
      return resp.json().then(function(data) {{
        errEl.textContent = data.detail || 'Something went wrong.';
        errEl.style.display = 'block';
      }});
    }}
  }}).catch(function() {{
    window.location.href = '/auth/start?invite=' + encodeURIComponent(code);
  }});
}}
</script>
</body>
</html>"""


def auth_success_html(username: str, manifest_url: str, user_key: str) -> str:
    """Success page shown after Trakt OAuth completes."""
    base_url = settings.base_url
    stremio_install = (
        f"stremio://{base_url.replace('https://', '').replace('http://', '')}"
        f"/{user_key}/manifest.json"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connected — Curatio</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,sans-serif;
  background:#0d0d0d;color:#e5e5e5;min-height:100vh;min-height:100dvh;
  display:flex;align-items:center;justify-content:center;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  -webkit-text-size-adjust:100%;
}}
.card{{
  max-width:500px;width:100%;margin:20px;padding:40px;
  background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;text-align:center;
}}
.check{{
  width:64px;height:64px;margin:0 auto 20px;background:#0d3d0d;
  border-radius:50%;display:flex;align-items:center;justify-content:center;
}}
.check svg{{stroke:#22c55e;width:32px;height:32px}}
h1{{font-size:1.5rem;margin-bottom:4px}}
h1 span{{color:#a855f7}}
.user{{color:#999;font-size:.95rem;margin-bottom:28px;line-height:1.4}}
.btn{{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:14px 28px;border-radius:10px;font-size:1rem;font-weight:600;
  cursor:pointer;border:none;text-decoration:none;transition:all .2s;width:100%;
  min-height:48px;-webkit-tap-highlight-color:transparent;
}}
.btn:active{{transform:scale(.98);opacity:.9}}
.btn-primary{{background:#e50914;color:#fff;margin-bottom:12px}}
.btn-primary:hover{{background:#c40812}}
.btn-secondary{{background:#2a2a2a;color:#e5e5e5;border:1px solid #444}}
.btn-secondary:hover{{background:#333}}
.manifest-url{{
  margin-top:20px;padding:12px 14px;background:#111;border:1px solid #333;
  border-radius:8px;font-family:monospace;font-size:.75rem;color:#666;
  word-break:break-all;user-select:all;-webkit-user-select:all;overflow-x:auto;
}}
.hint{{color:#555;font-size:.8rem;margin-top:12px;line-height:1.4}}
@media(max-width:600px){{
  body{{padding:16px}}
  .card{{padding:28px 20px;margin:12px;border-radius:12px}}
  .check{{width:56px;height:56px;margin-bottom:16px}}
  .check svg{{width:28px;height:28px}}
  h1{{font-size:1.3rem}}
  .user{{font-size:.9rem;margin-bottom:24px}}
  .btn{{padding:14px 20px;font-size:.95rem}}
  .manifest-url{{font-size:.7rem;padding:10px 12px}}
  .hint{{font-size:.75rem}}
}}
@media(max-width:360px){{
  .card{{padding:24px 16px}}
  .btn{{padding:12px 16px;font-size:.9rem}}
}}
</style>
</head>
<body>

<div class="card">
  <div class="check">
    <svg viewBox="0 0 24 24" fill="none" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  </div>

  <h1><span>CURATIO</span></h1>
  <p class="user">Connected as <strong>{username}</strong></p>

  <a class="btn btn-primary" href="{stremio_install}">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    Install in Stremio
  </a>

  <a class="btn btn-secondary" href="/">Back to Home</a>

  <div class="manifest-url">{manifest_url}</div>
  <p class="hint">Copy the manifest URL above if the button doesn't open Stremio.</p>
</div>

</body>
</html>"""


def auth_error_html(message: str) -> str:
    """Error page shown when Trakt OAuth fails."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Error — Curatio</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,sans-serif;
  background:#0d0d0d;color:#e5e5e5;min-height:100vh;min-height:100dvh;
  display:flex;align-items:center;justify-content:center;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  -webkit-text-size-adjust:100%;
}}
.card{{
  max-width:500px;width:100%;margin:20px;padding:40px;
  background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;text-align:center;
}}
.icon{{
  width:64px;height:64px;margin:0 auto 20px;background:#3d0d0d;
  border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:28px;
}}
h1{{font-size:1.5rem;margin-bottom:12px}}
p{{color:#999;font-size:.95rem;margin-bottom:24px;line-height:1.5}}
.btn{{
  display:inline-flex;align-items:center;justify-content:center;
  padding:14px 28px;border-radius:10px;font-size:1rem;font-weight:600;
  cursor:pointer;border:none;text-decoration:none;transition:all .2s;
  background:#2a2a2a;color:#e5e5e5;border:1px solid #444;width:100%;
  min-height:48px;-webkit-tap-highlight-color:transparent;
}}
.btn:hover{{background:#333}}
.btn:active{{transform:scale(.98);opacity:.9}}
@media(max-width:600px){{
  body{{padding:16px}}
  .card{{padding:28px 20px;margin:12px;border-radius:12px}}
  .icon{{width:56px;height:56px;font-size:24px;margin-bottom:16px}}
  h1{{font-size:1.3rem;margin-bottom:10px}}
  p{{font-size:.9rem;margin-bottom:20px}}
  .btn{{padding:14px 20px;font-size:.95rem}}
}}
@media(max-width:360px){{
  .card{{padding:24px 16px}}
  .btn{{padding:12px 16px;font-size:.9rem}}
}}
</style>
</head>
<body>

<div class="card">
  <div class="icon">&#10060;</div>
  <h1>Authentication Failed</h1>
  <p>{message}</p>
  <a class="btn" href="/">Try Again</a>
</div>

</body>
</html>"""
