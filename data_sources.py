"""
Resilient HTTP clients for the agentic-trader data sources.
Fixes the three failures from the 2026-06-25 scan report:
 - Reddit (401): proper application-only OAuth2 token + descriptive User-Agent
 - SEC Form 4 (403): declared User-Agent, per-IP rate limiting, sane backoff
 - STOCK Act (301): resolve + cache the redirect target instead of re-hitting
   the dead URL every scan

Required Railway env vars:
 REDDIT_CLIENT_ID (from https://www.reddit.com/prefs/apps, app type = "script")
 REDDIT_CLIENT_SECRET
 SEC_USER_AGENT e.g. "agentic-trader yourname@example.com" <-- not optional

Optional:
 REDDIT_USER_AGENT defaults to "agentic-trader/1.0"

Run `python data_sources.py` once after setting the vars to smoke-test all three.
"""
import os
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class DataSourceError(RuntimeError):
    """Typed failure so your circuit breaker can catch one clean exception type."""
    pass


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------
def make_session(user_agent: str, total_retries: int = 3) -> requests.Session:
    """A session that retries *transient* failures with exponential backoff.
    Deliberately does NOT retry 403: an SEC 403 is a header/policy problem or a
    ~10-minute IP block, neither of which clears within a few seconds of retries.
    """
    retry = Retry(
        total=total_retries,
        backoff_factor=1.0,  # waits 0s, 1s, 2s, 4s ...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": user_agent})
    return s


class RateLimiter:
    """Process-wide minimum interval between calls (thread-safe)."""
    def __init__(self, min_interval_s: float):
        self._min = min_interval_s
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            sleep_for = self._min - (time.monotonic() - self._last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# SEC EDGAR (Form 4 / insider transactions)
# ---------------------------------------------------------------------------
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
_sec_session = make_session(SEC_USER_AGENT or "agentic-trader MISSING-CONTACT@example.com")
_sec_limiter = RateLimiter(min_interval_s=0.15)  # ~6-7 req/s, comfortably under SEC's 10/s

# "getcurrent" returns the newest Form 4 filings across ALL companies — the right
# feed for an insider-buy scanner. Swap in whatever feed you were actually using.
SEC_LATEST_FORM4 = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count=40&output=atom"
)


def sec_get(url: str, **kwargs) -> requests.Response:
    """Fetch from SEC EDGAR with proper User-Agent and rate limiting."""
    if not SEC_USER_AGENT:
        raise DataSourceError(
            "SEC_USER_AGENT not set. EDGAR requires a 'Name email@domain' User-Agent "
            "or it returns 403 Forbidden."
        )
    _sec_limiter.wait()
    resp = _sec_session.get(url, timeout=15, **kwargs)
    if resp.status_code == 403:
        raise DataSourceError(
            "SEC 403: User-Agent rejected or IP temporarily blocked (~10 min). "
            "Confirm SEC_USER_AGENT is set and you're under ~8 req/s. This is almost "
            "certainly why Form 4 'stopped working' between yesterday and today."
        )
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Reddit (application-only OAuth2 — no user login needed for public reads)
# ---------------------------------------------------------------------------
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "agentic-trader/1.0")

_reddit_token = {"value": None, "expires_at": 0.0}
_reddit_lock = threading.Lock()


def _reddit_token_get() -> str:
    """Get or refresh Reddit OAuth2 token (thread-safe)."""
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        raise DataSourceError(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set -> HTTP 401."
        )
    with _reddit_lock:
        if _reddit_token["value"] and time.time() < _reddit_token["expires_at"] - 60:
            return _reddit_token["value"]
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        tok = resp.json()
        _reddit_token["value"] = tok["access_token"]
        _reddit_token["expires_at"] = time.time() + tok.get("expires_in", 3600)
        return _reddit_token["value"]


def reddit_get(path: str, **params) -> dict:
    """Fetch from Reddit OAuth API. e.g. reddit_get('/r/wallstreetbets/hot', limit=25)"""
    token = _reddit_token_get()
    resp = requests.get(
        f"https://oauth.reddit.com{path}",
        headers={"Authorization": f"bearer {token}", "User-Agent": REDDIT_USER_AGENT},
        params=params,
        timeout=15,
    )
    if resp.status_code == 401:
        _reddit_token["value"] = None  # force a fresh token next call
        raise DataSourceError(
            "Reddit 401 with a token present: check the app type is 'script' and "
            "the client id/secret match that app."
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# STOCK Act source (the 301)
# ---------------------------------------------------------------------------
def resolve_final_url(url: str, user_agent: str = "agentic-trader/1.0") -> str:
    """A persistent 301 means the endpoint permanently moved. Resolve it ONCE,
    log the chain, and put the returned final URL into your config so every scan
    hits the live endpoint directly instead of re-incurring the redirect (some
    clients also drop auth headers across a cross-host redirect)."""
    resp = requests.get(
        url,
        headers={"User-Agent": user_agent},
        allow_redirects=True,
        timeout=15
    )
    if resp.history:
        chain = " -> ".join(f"{r.status_code} {r.url}" for r in resp.history)
        print(f"[stock_act] redirect chain: {chain} -> FINAL {resp.url}")
    resp.raise_for_status()
    return resp.url


# ---------------------------------------------------------------------------
# Smoke test: run once on Railway after setting env vars
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 80)
    print("AGENTIC TRADER - DATA SOURCE SMOKE TEST")
    print("=" * 80)
    
    print("\n[1] SEC EDGAR (Form 4)")
    print(f"    SEC_USER_AGENT: {'set' if SEC_USER_AGENT else '*** MISSING ***'}")
    try:
        r = sec_get(SEC_LATEST_FORM4)
        print(f"    ✅ HTTP {r.status_code} OK ({len(r.content)} bytes)")
    except Exception as e:
        print(f"    ❌ FAILED: {e}")
    
    print("\n[2] Reddit OAuth2")
    creds = bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)
    print(f"    Credentials: {'set' if creds else '*** MISSING ***'}")
    try:
        data = reddit_get("/r/wallstreetbets/hot", limit=5)
        n = len(data.get("data", {}).get("children", []))
        print(f"    ✅ HTTP 200 OK ({n} posts)")
    except Exception as e:
        print(f"    ❌ FAILED: {e}")
    
    print("\n[3] STOCK Act (resolve redirect)")
    stock_act_url = "https://www.senate.gov/cgi-bin/fdsys/browse-edgar?action=getcurrent&type=4"
    try:
        final_url = resolve_final_url(stock_act_url)
        print(f"    ✅ Resolved to: {final_url}")
    except Exception as e:
        print(f"    ❌ FAILED: {e}")
    
    print("\n" + "=" * 80)
    print("SMOKE TEST COMPLETE")
    print("=" * 80)

