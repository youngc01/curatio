"""
HTML templates for the addon landing / configuration page.
"""

from app.config import settings


def landing_page_html() -> str:
    """Landing page with install options and Trakt connect."""
    base_url = settings.base_url
    manifest_url = f"{base_url}/manifest.json"
    stremio_install = f"stremio://{base_url.replace('https://', '').replace('http://', '')}/manifest.json"

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
  display:flex;flex-direction:column;align-items:center;
}}
.hero{{
  width:100%;padding:60px 20px 40px;text-align:center;
  background:linear-gradient(180deg,#1a1a2e 0%,#0d0d0d 100%);
}}
.hero h1{{font-size:2.4rem;font-weight:700;margin-bottom:8px;letter-spacing:2px}}
.hero h1 span{{color:#a855f7}}
.hero p{{color:#999;font-size:1.1rem;max-width:520px;margin:0 auto}}
.container{{max-width:680px;width:100%;padding:0 20px}}
.card{{
  background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;
  padding:32px;margin-top:28px;
}}
.card h2{{font-size:1.25rem;margin-bottom:6px}}
.card .subtitle{{color:#888;font-size:.9rem;margin-bottom:20px}}
.btn{{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:12px 28px;border-radius:8px;font-size:1rem;font-weight:600;
  cursor:pointer;border:none;text-decoration:none;transition:all .2s;
  width:100%;
}}
.btn-primary{{background:#a855f7;color:#fff}}
.btn-primary:hover{{background:#7c3aed}}
.btn-secondary{{background:#2a2a2a;color:#e5e5e5;border:1px solid #444}}
.btn-secondary:hover{{background:#333}}
.btn-trakt{{background:#ed1c24;color:#fff}}
.btn-trakt:hover{{background:#c8171e}}
.divider{{
  display:flex;align-items:center;gap:16px;margin:28px 0;color:#555;font-size:.85rem;
}}
.divider::before,.divider::after{{content:'';flex:1;border-top:1px solid #333}}
.input-group{{margin-bottom:16px}}
.input-group label{{display:block;font-size:.85rem;color:#999;margin-bottom:6px}}
.input-group input{{
  width:100%;padding:12px 14px;background:#111;border:1px solid #333;
  border-radius:8px;color:#e5e5e5;font-size:.95rem;outline:none;
}}
.input-group input:focus{{border-color:#a855f7}}
.error-msg{{
  color:#f87171;font-size:.85rem;margin-top:8px;display:none;
}}
.features{{
  display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:28px;
}}
.feature{{
  background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:20px;
}}
.feature .icon{{font-size:1.6rem;margin-bottom:8px}}
.feature h3{{font-size:.95rem;margin-bottom:4px}}
.feature p{{color:#888;font-size:.8rem;line-height:1.4}}
.manifest-url{{
  margin-top:12px;padding:10px 14px;background:#111;border:1px solid #333;
  border-radius:8px;font-family:monospace;font-size:.8rem;color:#888;
  word-break:break-all;user-select:all;
}}
footer{{
  margin-top:auto;padding:32px 20px;text-align:center;
  color:#555;font-size:.8rem;
}}
footer a{{color:#888;text-decoration:none}}
footer a:hover{{color:#e5e5e5}}
@media(max-width:520px){{
  .hero h1{{font-size:1.6rem}}
  .features{{grid-template-columns:1fr}}
  .card{{padding:24px 18px}}
}}
</style>
</head>
<body>

<div class="hero">
  <h1><span>CURATIO</span></h1>
  <p>AI-curated cinema for Stremio. 40 curated catalogs with 150k+ movies and shows.</p>
</div>

<div class="container">

  <!-- Quick Install Card -->
  <div class="card">
    <h2>Install Addon</h2>
    <p class="subtitle">Get 40 AI-curated catalogs instantly — no account required.</p>
    <a class="btn btn-primary" href="{stremio_install}">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      Install in Stremio
    </a>
    <div class="manifest-url">{manifest_url}</div>
  </div>

  <div class="divider">or personalise with Trakt</div>

  <!-- Trakt Connect Card -->
  <div class="card">
    <h2>Connect Trakt Account</h2>
    <p class="subtitle">Link your Trakt history to unlock personalized recommendations tailored to your taste.</p>

    <form id="trakt-form" onsubmit="return startTraktAuth(event)">
      <div class="input-group">
        <label for="password">Addon Password</label>
        <input type="password" id="password" name="password" placeholder="Enter your addon password" required autocomplete="current-password">
      </div>
      <button class="btn btn-trakt" type="submit">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 012 2v14a2 2 0 01-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
        Connect with Trakt
      </button>
      <p class="error-msg" id="error-msg"></p>
    </form>
  </div>

  <!-- Feature Grid -->
  <div class="features">
    <div class="feature">
      <div class="icon">&#127916;</div>
      <h3>40 Curated Catalogs</h3>
      <p>From Dark Crime Dramas to Cyberpunk Futures — browse by mood, era, and style.</p>
    </div>
    <div class="feature">
      <div class="icon">&#129302;</div>
      <h3>AI-Powered Tagging</h3>
      <p>Every title tagged with semantic labels by AI for precise, nuanced matching.</p>
    </div>
    <div class="feature">
      <div class="icon">&#128202;</div>
      <h3>Trakt Integration</h3>
      <p>Connect your watch history for recommendations that adapt to your taste.</p>
    </div>
    <div class="feature">
      <div class="icon">&#128260;</div>
      <h3>Daily Updates</h3>
      <p>New releases are tagged and slotted into catalogs automatically every day.</p>
    </div>
  </div>
</div>

<footer>
  Curatio &middot; Powered by Gemini &amp; TMDB
</footer>

<script>
function startTraktAuth(e) {{
  e.preventDefault();
  var pw = document.getElementById('password').value;
  if (!pw) return false;
  var errEl = document.getElementById('error-msg');
  errEl.style.display = 'none';

  // Verify password first, then redirect
  fetch('/auth/start?password=' + encodeURIComponent(pw), {{
    method: 'GET',
    redirect: 'manual'
  }}).then(function(resp) {{
    if (resp.type === 'opaqueredirect' || resp.status === 307 || resp.status === 302 || resp.status === 303) {{
      // Redirect to Trakt OAuth
      window.location.href = '/auth/start?password=' + encodeURIComponent(pw);
    }} else if (resp.status === 403) {{
      errEl.textContent = 'Invalid password. Please try again.';
      errEl.style.display = 'block';
    }} else {{
      return resp.json().then(function(data) {{
        errEl.textContent = data.detail || 'Something went wrong.';
        errEl.style.display = 'block';
      }});
    }}
  }}).catch(function() {{
    // fetch with redirect:'manual' may throw — just navigate directly
    window.location.href = '/auth/start?password=' + encodeURIComponent(pw);
  }});

  return false;
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
  background:#0d0d0d;color:#e5e5e5;min-height:100vh;
  display:flex;align-items:center;justify-content:center;
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
.user{{color:#999;font-size:.95rem;margin-bottom:28px}}
.btn{{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:14px 28px;border-radius:8px;font-size:1rem;font-weight:600;
  cursor:pointer;border:none;text-decoration:none;transition:all .2s;width:100%;
}}
.btn-primary{{background:#e50914;color:#fff;margin-bottom:12px}}
.btn-primary:hover{{background:#c40812}}
.btn-secondary{{background:#2a2a2a;color:#e5e5e5;border:1px solid #444}}
.btn-secondary:hover{{background:#333}}
.manifest-url{{
  margin-top:20px;padding:10px 14px;background:#111;border:1px solid #333;
  border-radius:8px;font-family:monospace;font-size:.75rem;color:#666;
  word-break:break-all;user-select:all;
}}
.hint{{color:#555;font-size:.8rem;margin-top:12px}}
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
  background:#0d0d0d;color:#e5e5e5;min-height:100vh;
  display:flex;align-items:center;justify-content:center;
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
p{{color:#999;font-size:.95rem;margin-bottom:24px}}
.btn{{
  display:inline-flex;align-items:center;justify-content:center;
  padding:12px 28px;border-radius:8px;font-size:1rem;font-weight:600;
  cursor:pointer;border:none;text-decoration:none;transition:all .2s;
  background:#2a2a2a;color:#e5e5e5;border:1px solid #444;width:100%;
}}
.btn:hover{{background:#333}}
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
