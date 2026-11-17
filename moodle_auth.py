import json
import tempfile
from typing import TypedDict
from urllib.parse import urlparse

import requests
from playwright.sync_api import BrowserContext, Page, sync_playwright


class MoodleCredentials(TypedDict):
    base_url: str
    cookie_name: str
    MoodleSession: str
    sesskey: str


def _find_session_cookie(context: BrowserContext, moodle_base_url: str) -> tuple[str, str] | None:
    """Returns (cookie_name, cookie_value) for the first MoodleSession* cookie
    visible to moodle_base_url, or None if there isn't one yet."""
    for cookie in context.cookies(moodle_base_url):
        c_name = cookie.get("name")
        c_val = cookie.get("value")
        if c_name and c_val and c_name.startswith("MoodleSession"):
            return c_name, c_val
    return None


def _is_authenticated(moodle_base_url: str, cookie_name: str, moodle_session: str) -> bool:
    """Confirms the cookie is a real logged-in session rather than an anonymous
    one. Moodle's /my/ dashboard requires login and redirects anonymous
    sessions to /login/ (or an external SSO domain), so this works regardless
    of Moodle version/theme."""
    domain = urlparse(moodle_base_url).netloc
    probe = requests.Session()
    probe.cookies.set(cookie_name, moodle_session, domain=domain)
    try:
        resp = probe.get(f"{moodle_base_url}/my/", timeout=10, allow_redirects=True)
    except requests.RequestException:
        return False
    final_url = urlparse(resp.url)
    return (
        resp.status_code == 200
        and final_url.netloc == domain
        and not final_url.path.startswith("/login")
    )


def capture_moodle_session(moodle_base_url: str) -> MoodleCredentials:
    """Opens a chromeless app window for login and returns MoodleSession + sesskey.

    Works for direct username/password logins AND SSO flows (CAS/USOS, Shibboleth,
    OAuth2, ...) that bounce the browser through a different domain before coming
    back to Moodle.
    """
    moodle_base_url = moodle_base_url.rstrip("/")
    target_domain = urlparse(moodle_base_url).netloc
    login_url = f"{moodle_base_url}/login/index.php"

    with tempfile.TemporaryDirectory() as tmp_profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=tmp_profile,
            headless=False,
            no_viewport=True,
            args=[
                f"--app={login_url}",
                "--window-size=900,700",
            ],
        )

        # Track every tab the SSO flow opens, not just the first one - some
        # "log in with ..." buttons open the identity provider in a popup
        # rather than navigating the same tab.
        pages: list[Page] = list(context.pages) or [context.new_page()]
        context.on("page", lambda new_page: pages.append(new_page))

        if pages[0].url == "about:blank":
            pages[0].goto(login_url)

        print(f"[*] Waiting for user to log in at {moodle_base_url}...")

        last_seen_urls: dict[Page, str] = {}
        last_attempt: tuple[str, str, str] | None = None

        while True:
            live_pages = [pg for pg in pages if not pg.is_closed()]
            if not live_pages:
                raise RuntimeError("Login window was closed before authentication completed.")

            for page in live_pages:
                try:
                    if last_seen_urls.get(page) != page.url:
                        print(f"    ...currently on {page.url}")
                        last_seen_urls[page] = page.url

                    parsed_url = urlparse(page.url)
                    if parsed_url.netloc != target_domain:
                        continue  # still on the SSO/identity-provider domain

                    # Wait until CAS callback redirects past /login/ so we don't
                    # read a guest sesskey from the login page DOM.
                    if parsed_url.path.startswith("/login"):
                        continue

                    sesskey = page.evaluate(
                        "() => (window.M && window.M.cfg && window.M.cfg.sesskey) || null"
                    )
                    if not isinstance(sesskey, str) or not sesskey:
                        continue

                    found = _find_session_cookie(context, moodle_base_url)
                    if not found:
                        continue
                    cookie_name, moodle_session = found

                    # Don't hammer the server on every 0.5s tick - only re-check
                    # when the cookie or sesskey actually changes.
                    attempt = (cookie_name, moodle_session, sesskey)
                    if attempt == last_attempt:
                        continue
                    last_attempt = attempt

                    if _is_authenticated(moodle_base_url, cookie_name, moodle_session):
                        context.close()
                        return {
                            "base_url": moodle_base_url,
                            "cookie_name": cookie_name,
                            "MoodleSession": moodle_session,
                            "sesskey": sesskey,
                        }
                except Exception:
                    # Ignore execution context destruction during redirects, etc.
                    pass

            # Use Playwright's wait_for_timeout instead of time.sleep so the
            # internal greenlet event loop processes navigation events!
            try:
                live_pages[0].wait_for_timeout(500)
            except Exception:
                pass


def create_authenticated_client(creds: MoodleCredentials) -> requests.Session:
    """Builds a requests.Session pre-loaded with the hijacked MoodleSession."""
    session = requests.Session()
    domain = urlparse(creds["base_url"]).netloc
    session.cookies.set(creds["cookie_name"], creds["MoodleSession"], domain=domain)
    return session


if __name__ == "__main__":
    MOODLE_URL = "https://leon.pw.edu.pl"

    creds = capture_moodle_session(MOODLE_URL)
    print("\n[+] Successfully captured credentials:")
    print(json.dumps(creds, indent=2))

    http = create_authenticated_client(creds)
    ajax_url = f"{creds['base_url']}/lib/ajax/service.php?sesskey={creds['sesskey']}&info=core_session_time_remaining"
    payload = [{"index": 0, "methodname": "core_session_time_remaining", "args": {}}]

    resp = http.post(ajax_url, json=payload)
    print("\n[*] Session validation response from Moodle AJAX:")
    print(resp.json())
