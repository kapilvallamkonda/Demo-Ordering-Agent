"""Optional outside tools (phase 2 of analytics): PostHog in the browser, Sentry on the server.

Both are off unless configured, and both are set up to keep customer data out:
  - PostHog (POSTHOG_KEY, POSTHOG_HOST): page views, funnels and session replays on our own
    pages. No one is identified by email. On the business pages (/manage) replays mask all
    text and inputs, so menus, phone numbers and call transcripts never leave our server;
    the public home page records normally.
  - Sentry (SENTRY_DSN): every error the server logs (log.exception) becomes an issue with
    its stack trace; requests and Claude calls are traced, logs are sent. No user data, and no
    Claude prompts or replies (they hold callers' words).
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("phone_agent.web_analytics")


def posthog_config() -> dict | None:
    key = os.environ.get("POSTHOG_KEY", "").strip()
    if not key:
        return None
    host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").strip().rstrip("/")
    return {"key": key, "host": host, "assets": host.replace(".i.posthog.com", "-assets.i.posthog.com")}


def csp_extra() -> dict:
    """Extra Content-Security-Policy sources the pages need when PostHog is on."""
    cfg = posthog_config()
    if not cfg:
        return {}
    return {"script-src": [cfg["assets"]], "connect-src": [cfg["host"], cfg["assets"]],
            "worker-src": ["blob:"]}


def loader_js(page: str) -> str:
    """/web-analytics.js: a queue stub that loads PostHog and starts it (empty when off)."""
    cfg = posthog_config()
    if not cfg:
        return "/* web analytics off */\n"
    options = {"api_host": cfg["host"], "person_profiles": "identified_only", "capture_pageview": True,
               "session_recording": {"maskAllInputs": True,
                                     **({"maskTextSelector": "*"} if page == "manage" else {})}}
    return (
        "(function(){var w=window,d=document;if(w.posthog&&w.posthog.__SV)return;"
        "var p=w.posthog=[];p._i=[];p.__SV=1;"
        "['capture','identify','reset','register','startSessionRecording'].forEach(function(m){"
        "p[m]=function(){p.push([m].concat([].slice.call(arguments)))}});"
        f"p._i.push([{json.dumps(cfg['key'])},{json.dumps(options)},'posthog']);"
        "var s=d.createElement('script');s.async=true;s.crossOrigin='anonymous';"
        f"s.src={json.dumps(cfg['assets'] + '/static/array.js')};d.head.appendChild(s);}})();\n"
    )


def init_sentry() -> bool:
    """Sentry for the server (project ringcart-server), when SENTRY_DSN is set:
    - errors: uncaught exceptions, plus every ERROR log record (the app catches and logs most
      failures, e.g. a POS send or a payment link, so they'd otherwise never reach Sentry)
    - tracing: requests and Claude calls; 1.0 outside production, 0.1 in production
      (SENTRY_TRACES_SAMPLE_RATE overrides)
    - logs: INFO and above as Sentry logs, searchable next to the errors
    Privacy: no user data (IPs, cookies), no request bodies or variable values, and no
    prompts or replies from Claude calls, which hold callers' words and numbers; only
    timings and token counts."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import logging as _logging

        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        environment = os.environ.get("SENTRY_ENVIRONMENT", "production")
        default_rate = "0.1" if environment == "production" else "1.0"
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=os.environ.get("SENTRY_RELEASE") or os.environ.get("FLY_IMAGE_REF") or None,
            # SDK 2.x defaults with send_default_pii off: no IPs, cookies, user data or Claude
            # prompts and replies. Request bodies and local variables can hold menus, phone
            # numbers and what callers said, so they stay out too.
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", default_rate)),
            enable_logs=True,
            integrations=[LoggingIntegration(level=_logging.INFO, event_level=_logging.ERROR)],
        )
        log.info("Sentry on (%s)", environment)
        return True
    except Exception:
        log.exception("couldn't start Sentry")
        return False
