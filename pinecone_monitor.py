#!/usr/bin/env python3
"""
Pinecone Index Monitor
======================
Checks all configured Pinecone projects every 2 hours for anomalies
and sends email alerts.

HOW DROP DETECTION WORKS (handles delta update pipelines):
  Delta updates delete first, then re-add -- so the vector count temporarily
  drops before recovering within a few hours. To avoid false alarms, this
  monitor uses a grace period: a drop is recorded but no email is sent until
  the count has been low for DROP_GRACE_HOURS (default: 6h) without recovering.
  If it recovers in time, nothing is sent. If still low after the grace period,
  you get the alert.

Alert conditions:
  - Vector count dropped >DROP_THRESHOLD (default 20%) for >DROP_GRACE_HOURS (6h)
    without recovering  ->  "stuck pipeline"
  - Index vector count unchanged for >STALE_DAYS (default 7 days)
  - Index completely empty (0 vectors) for >DROP_GRACE_HOURS without recovering

Email is sent via Resend (https://resend.com) -- free account, verify your domain.

Required GitHub Actions secrets:
  PINECONE_PROJECTS  -- JSON array: [{"name":"teamny","api_key":"pcsk_..."},...]
  RESEND_API_KEY     -- Your Resend API key (re_...)
  ALERT_EMAIL        -- Where to send alerts (e.g. team@dialogintelligens.dk)
  FROM_EMAIL         -- Sender at your verified domain (e.g. monitor@dialogintelligens.dk)

Optional env vars (with defaults):
  DROP_THRESHOLD     -- Float (default 0.20 = 20%) -- tracks drops above this size
  DROP_GRACE_HOURS   -- Float (default 6.0) -- alert fires only if drop lasts this long
  STALE_DAYS         -- Int (default 7) -- alert if count unchanged this many days
"""

import json
import os
import sys
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

ALERT_EMAIL     = os.environ.get("ALERT_EMAIL",     "team@dialogintelligens.dk")
FROM_EMAIL      = os.environ.get("FROM_EMAIL",      "monitor@dialogintelligens.dk")
RESEND_API_KEY  = os.environ.get("RESEND_API_KEY",  "")

SMTP_HOST     = os.environ.get("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER     = os.environ.get("SMTP_USER",     "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

DROP_THRESHOLD   = float(os.environ.get("DROP_THRESHOLD",   "0.20"))
DROP_GRACE_HOURS = float(os.environ.get("DROP_GRACE_HOURS", "6.0"))
STALE_DAYS       = int(os.environ.get("STALE_DAYS",         "7"))

PROJECTS_JSON = os.environ.get("PINECONE_PROJECTS", "[]")
STATE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pinecone_state.json")

PINECONE_API_BASE = "https://api.pinecone.io"


# ------------------------------------------------------------------------------
# State persistence
# ------------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    print("  State saved -> " + STATE_FILE)


# ------------------------------------------------------------------------------
# Pinecone API helpers (no external libraries needed)
# ------------------------------------------------------------------------------

def pinecone_request(method, url, api_key, body=None):
    """Make a Pinecone REST API call and return parsed JSON."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Api-Key": api_key,
        "Content-Type": "application/json",
        "X-Pinecone-API-Version": "2025-04",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP {}: {}".format(e.code, e.read().decode()))
    except urllib.error.URLError as e:
        raise RuntimeError("Network error: {}".format(e.reason))


def list_indexes(api_key):
    result = pinecone_request("GET", PINECONE_API_BASE + "/indexes", api_key)
    return result.get("indexes", [])


def get_index_stats(host, api_key):
    url = "https://{}/describe_index_stats".format(host)
    return pinecone_request("POST", url, api_key, body={})


# ------------------------------------------------------------------------------
# Core monitoring logic
# ------------------------------------------------------------------------------

def check_project(project_name, api_key, state, alerts):
    print("\n" + "-" * 55)
    print("  Project: " + project_name)
    print("-" * 55)

    try:
        indexes = list_indexes(api_key)
    except RuntimeError as e:
        msg = "Cannot list indexes: {}".format(e)
        print("  FAIL: " + msg)
        alerts.append({
            "type": "unreachable_project",
            "project": project_name,
            "index": "-",
            "message": msg,
        })
        return

    print("  Found {} index(es)".format(len(indexes)))

    if project_name not in state:
        state[project_name] = {}
    project_state = state[project_name]
    now_dt  = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    active_index_names = set()

    for idx in indexes:
        index_name = idx.get("name", "unknown")
        host       = idx.get("host", "")
        active_index_names.add(index_name)

        print("\n    Index: " + index_name)

        if not host:
            print("      SKIP: no host in API response")
            continue

        # --- Get current vector count ---
        try:
            stats         = get_index_stats(host, api_key)
            current_count = stats.get("totalVectorCount", stats.get("total_vector_count", 0))
            print("      Vectors: {:,}".format(current_count))
        except RuntimeError as e:
            msg = "Cannot get stats: {}".format(e)
            print("      FAIL: " + msg)
            alerts.append({
                "type": "unreachable_index",
                "project": project_name,
                "index": index_name,
                "message": msg,
            })
            continue

        # --- Load previous state for this index ---
        prev             = project_state.get(index_name, {})
        prev_count       = prev.get("last_vector_count")     # None on first run
        last_changed_iso = prev.get("last_changed_at")
        drop_detected_at = prev.get("drop_detected_at")      # ISO str or None
        drop_from_count  = prev.get("drop_from_count")       # int or None

        # --- Is the current count significantly lower than last run? ---
        is_significant_drop = False
        if prev_count is not None and prev_count > 0:
            drop_frac = (prev_count - current_count) / prev_count
            is_significant_drop = (drop_frac >= DROP_THRESHOLD) or (current_count == 0)
        elif prev_count == 0 and current_count == 0 and drop_detected_at:
            is_significant_drop = True  # still empty, ongoing

        # --- Drop state machine ---
        if is_significant_drop and drop_detected_at is None:
            # First time we see this drop -- start grace period, no alert yet
            drop_detected_at = now_iso
            drop_from_count  = prev_count
            print("      [WATCH] Drop detected -- grace period started ({:.0f}h until alert if not recovered)".format(
                DROP_GRACE_HOURS))

        elif drop_detected_at is not None:
            detected_dt   = datetime.fromisoformat(drop_detected_at)
            hours_elapsed = (now_dt - detected_dt).total_seconds() / 3600
            baseline      = drop_from_count or 0

            # Recovery = count climbed back above (1 - threshold) of original baseline
            recovery_floor = baseline * (1 - DROP_THRESHOLD)
            recovered = (current_count >= recovery_floor) and (current_count > 0)

            if recovered:
                # Recovered within grace period -- clear state, no alert
                print("      [OK] Recovered: {:,} -> {:,} in {:.1f}h -- no alert sent".format(
                    baseline, current_count, hours_elapsed))
                drop_detected_at = None
                drop_from_count  = None

            elif hours_elapsed >= DROP_GRACE_HOURS:
                # Grace period expired, still low -- fire alert
                actual_pct = (baseline - current_count) / baseline if baseline > 0 else 1.0
                if current_count == 0:
                    msg        = "Index empty for {:.0f}h (was {:,} vectors) -- pipeline may be stuck".format(
                        hours_elapsed, baseline)
                    alert_type = "empty_index"
                else:
                    msg        = "Down {:.1%} for {:.0f}h ({:,} -> {:,}) -- not recovering".format(
                        actual_pct, hours_elapsed, baseline, current_count)
                    alert_type = "big_drop"

                print("      [ALERT] " + msg)
                alerts.append({
                    "type":           alert_type,
                    "project":        project_name,
                    "index":          index_name,
                    "previous_count": baseline,
                    "current_count":  current_count,
                    "drop_pct":       round(actual_pct * 100, 1),
                    "hours_elapsed":  round(hours_elapsed, 1),
                    "message":        msg,
                })
                # Keep drop_detected_at so we don't spam the same alert every 2h.
                # It clears only when the index recovers.

            else:
                remaining = DROP_GRACE_HOURS - hours_elapsed
                print("      [WAIT] Drop ongoing -- {:.1f}h elapsed, {:.1f}h until alert".format(
                    hours_elapsed, remaining))

        else:
            if prev_count is not None:
                print("      [OK]")

        # --- Stale check (only when not in a drop phase) ---
        if drop_detected_at is None and last_changed_iso and current_count == prev_count:
            last_changed_dt   = datetime.fromisoformat(last_changed_iso)
            days_since_change = (now_dt - last_changed_dt).days
            if days_since_change >= STALE_DAYS:
                msg = "No change for {} days (count: {:,}) -- updates may have stopped".format(
                    days_since_change, current_count)
                print("      [ALERT] STALE -- " + msg)
                alerts.append({
                    "type":          "stale_index",
                    "project":       project_name,
                    "index":         index_name,
                    "current_count": current_count,
                    "days_stale":    days_since_change,
                    "last_changed":  last_changed_iso,
                    "message":       msg,
                })

        # --- Update state ---
        if prev_count is None:
            new_last_changed = now_iso           # first run
        elif current_count != prev_count:
            new_last_changed = now_iso           # count moved
        else:
            new_last_changed = last_changed_iso  # unchanged

        project_state[index_name] = {
            "last_vector_count": current_count,
            "last_changed_at":   new_last_changed or now_iso,
            "last_checked_at":   now_iso,
            "drop_detected_at":  drop_detected_at,
            "drop_from_count":   drop_from_count,
        }

    # Remove state entries for indexes that were deleted
    for gone in list(project_state.keys()):
        if gone not in active_index_names:
            print("\n    [INFO] Index '{}' no longer exists -- removed from state".format(gone))
            del project_state[gone]


# ------------------------------------------------------------------------------
# Email
# ------------------------------------------------------------------------------

def build_html_email(alerts):
    BADGE = {
        "empty_index":         ("#dc2626", "Empty Index"),
        "big_drop":            ("#ea580c", "Drop Not Recovering"),
        "stale_index":         ("#ca8a04", "Stale - No Updates"),
        "unreachable_index":   ("#6b7280", "Unreachable Index"),
        "unreachable_project": ("#374151", "Unreachable Project"),
    }

    rows = ""
    for a in alerts:
        color, label = BADGE.get(a["type"], ("#6b7280", "Alert"))
        rows += (
            "<tr>"
            "<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;white-space:nowrap;'>"
            "<span style='background:{color};color:#fff;padding:3px 9px;border-radius:4px;"
            "font-size:12px;font-weight:600;'>{label}</span></td>"
            "<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;"
            "font-family:monospace;font-size:13px;'>{project}</td>"
            "<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;"
            "font-family:monospace;font-size:13px;'>{index}</td>"
            "<td style='padding:10px 12px;border-bottom:1px solid #e5e7eb;"
            "font-size:13px;'>{message}</td>"
            "</tr>"
        ).format(
            color=color, label=label,
            project=a.get("project", ""),
            index=a.get("index", ""),
            message=a.get("message", ""),
        )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    count   = len(alerts)

    return (
        "<!DOCTYPE html><html><body style='font-family:-apple-system,BlinkMacSystemFont,"
        "Segoe UI,sans-serif;background:#f9fafb;margin:0;padding:24px;'>"
        "<div style='max-width:900px;margin:0 auto;background:#fff;border-radius:8px;"
        "box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden;'>"
        "<div style='background:#dc2626;padding:20px 24px;'>"
        "<h2 style='margin:0;color:#fff;font-size:20px;'>Pinecone Monitor -- {count} issue(s) detected</h2>"
        "<p style='margin:6px 0 0;color:#fecaca;font-size:13px;'>{now}</p>"
        "</div>"
        "<div style='padding:24px;'>"
        "<table style='width:100%;border-collapse:collapse;'>"
        "<thead><tr style='background:#f3f4f6;'>"
        "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;"
        "border-bottom:2px solid #e5e7eb;'>TYPE</th>"
        "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;"
        "border-bottom:2px solid #e5e7eb;'>PROJECT</th>"
        "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;"
        "border-bottom:2px solid #e5e7eb;'>INDEX</th>"
        "<th style='padding:10px 12px;text-align:left;font-size:12px;color:#6b7280;"
        "border-bottom:2px solid #e5e7eb;'>DETAILS</th>"
        "</tr></thead>"
        "<tbody>{rows}</tbody>"
        "</table>"
        "<p style='margin-top:20px;font-size:12px;color:#9ca3af;'>"
        "Sent by Pinecone Monitor | GitHub Actions | DialogIntelligens</p>"
        "</div></div></body></html>"
    ).format(count=count, now=now_str, rows=rows)


def send_via_resend(api_key, to_email, from_email, subject, html):
    payload = json.dumps({
        "from":    "Pinecone Monitor <{}>".format(from_email),
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": "Bearer {}".format(api_key),
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print("  Email sent via Resend (id={})".format(result.get("id")))
            return True
    except urllib.error.HTTPError as e:
        print("  Resend error {}: {}".format(e.code, e.read().decode()))
        return False


def send_via_smtp(host, port, user, password, from_email, to_email, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = "Pinecone Monitor <{}>".format(from_email)
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL(host, port) as server:
            server.login(user, password)
            server.sendmail(from_email, to_email, msg.as_string())
        print("  Email sent via SMTP to " + to_email)
        return True
    except Exception as e:
        print("  SMTP error: {}".format(e))
        return False


def dispatch_alert(alerts):
    subject = "Pinecone Alert: {} issue(s) detected".format(len(alerts))
    html    = build_html_email(alerts)

    if RESEND_API_KEY:
        return send_via_resend(RESEND_API_KEY, ALERT_EMAIL, FROM_EMAIL, subject, html)
    elif SMTP_USER and SMTP_PASSWORD:
        return send_via_smtp(SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
                             FROM_EMAIL, ALERT_EMAIL, subject, html)
    else:
        print("  No email credentials set -- printing summary only")
        for a in alerts:
            print("  [{}] {}/{}: {}".format(a["type"], a.get("project",""), a.get("index",""), a.get("message","")))
        return False


# ------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  Pinecone Index Monitor")
    print("  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    print("=" * 55)
    print("  Alert email  : " + ALERT_EMAIL)
    print("  Drop track   : {:.0%}+ drop starts grace period".format(DROP_THRESHOLD))
    print("  Grace period : {:.0f}h (alert fires only if not recovered)".format(DROP_GRACE_HOURS))
    print("  Stale after  : {} days".format(STALE_DAYS))
    print("  Email via    : {}".format("Resend" if RESEND_API_KEY else ("SMTP" if SMTP_USER else "NOT CONFIGURED")))

    try:
        projects = json.loads(PROJECTS_JSON)
    except json.JSONDecodeError as e:
        print("\nERROR: Invalid PINECONE_PROJECTS JSON: " + str(e))
        sys.exit(1)

    if not projects:
        print("\nWARNING: No projects in PINECONE_PROJECTS. Nothing to check.")
        sys.exit(0)

    print("\n  Checking {} project(s)...\n".format(len(projects)))

    state  = load_state()
    alerts = []

    for project in projects:
        name    = project.get("name", "unnamed")
        api_key = project.get("api_key", "")
        if not api_key:
            print("  WARNING: No api_key for {} -- skipping".format(name))
            continue
        check_project(name, api_key, state, alerts)

    save_state(state)

    print("\n" + "=" * 55)
    print("  Done. {} alert(s) found.".format(len(alerts)))
    print("=" * 55 + "\n")

    if alerts:
        print("  Sending alert email...")
        ok = dispatch_alert(alerts)
        if not ok:
            sys.exit(1)
    else:
        print("  All indexes healthy -- no alerts.")


if __name__ == "__main__":
    main()
