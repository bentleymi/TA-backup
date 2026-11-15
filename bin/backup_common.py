"""Shared collection logic for TA-backup scripted inputs (restic/borg + timers)."""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

CONF = os.path.join(
    os.environ.get("SPLUNK_HOME", "/opt/splunk"),
    "etc", "apps", "TA-backup", "local", "backup.conf",
)
CONF_KEYS = ("restic_repository", "restic_password", "borg_repository", "borg_passphrase")

TIMER_RE = re.compile(r"(restic|borg|duplicity|rclone|timeshift|s3cmd|b2|backup)", re.IGNORECASE)
TIMER_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
DATE_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_TOKEN = re.compile(r"^\d{2}:\d{2}:\d{2}$")

ALARM_AGE_HOURS = 72.0
TIMER_STALE_HOURS = 24.0


def read_conf():
    conf = {}
    try:
        with open(CONF) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key in CONF_KEYS and val:
                    conf[key] = val
    except OSError:
        pass
    return conf


def run_cmd(cmd, env=None):
    try:
        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                               stdin=subprocess.DEVNULL, env=e)
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_json_blob(p):
    if p is None or p.returncode != 0:
        return None
    for stream in (p.stdout, p.stderr):
        if not stream:
            continue
        try:
            return json.loads(stream)
        except ValueError:
            continue
    return None


def parse_iso(ts):
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def age_hours(last, now):
    try:
        return round((now - last.timestamp()) / 3600.0, 2)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _collect_restdic(conf):
    repo = conf.get("restic_repository")
    if not repo:
        return {"configured": 0}
    rec = {"configured": 1}
    env = {}
    if conf.get("restic_password"):
        env["RESTIC_PASSWORD"] = conf["restic_password"]
    data = parse_json_blob(run_cmd(["restic", "snapshots", "--json", "--repo", repo], env=env))
    if not isinstance(data, list):
        return rec
    rec["snapshots"] = len(data)
    latest = None
    latest_dt = None
    for snap in data:
        if not isinstance(snap, dict):
            continue
        dt = parse_iso(snap.get("time"))
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest = snap["time"]
    if latest is not None:
        rec["last_ts"] = latest
        rec["age_hours"] = age_hours(latest_dt, time.time())
    return rec


def _collect_borg(conf):
    repo = conf.get("borg_repository")
    if not repo:
        return {"configured": 0}
    rec = {"configured": 1}
    env = {}
    if conf.get("borg_passphrase"):
        env["BORG_PASSPHRASE"] = conf["borg_passphrase"]
    data = parse_json_blob(run_cmd(["borg", "list", "--json", repo], env=env))
    if not isinstance(data, dict):
        return rec
    rec["snapshots"] = len(data)
    latest = None
    latest_dt = None
    for key in data:
        dt = parse_iso(key)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest = key
    if latest is not None:
        rec["last_ts"] = latest
        rec["age_hours"] = age_hours(latest_dt, time.time())
    return rec


def _timer_dt(s):
    m = TIMER_TS.match(s or "")
    if not m:
        return None
    try:
        return datetime(*[int(g) for g in m.groups()])
    except ValueError:
        return None


def _timer_row(parts):
    if len(parts) < 4:
        return None
    stamps = []
    for j in range(len(parts) - 1):
        if DATE_TOKEN.match(parts[j]) and TIME_TOKEN.match(parts[j + 1]):
            stamps.append(j)
            if len(stamps) == 2:
                break
    nxt = parts[stamps[0]] + " " + parts[stamps[0] + 1] if stamps else None
    last = parts[stamps[1]] + " " + parts[stamps[1] + 1] if len(stamps) > 1 else None
    return nxt, last, parts[-2]


def _list_timers():
    cmd = ["systemctl", "list-timers", "--all", "--no-legend",
           "--no-pager", "--plain", "--output=full"]
    for c in (cmd, cmd[:-1]):
        p = run_cmd(c)
        if p is not None and p.returncode == 0 and (p.stdout or "").strip():
            return p.stdout
    return None


def collect_timers():
    out = {"backup_timers": 0, "timers_stale": 0, "stale_timers": []}
    text = _list_timers()
    if text is None:
        return out
    now = datetime.now()
    stale_after = timedelta(hours=TIMER_STALE_HOURS)
    stale = []
    for line in text.splitlines():
        row = _timer_row(line.split())
        if row is None:
            continue
        nxt, last, unit = row
        if not TIMER_RE.search(unit):
            continue
        out["backup_timers"] += 1
        last_dt = _timer_dt(last)
        next_dt = _timer_dt(nxt)
        if last_dt is not None and next_dt is not None and now - last_dt > stale_after and next_dt < now:
            stale.append(unit)
    out["stale_timers"] = sorted(stale)
    out["timers_stale"] = len(stale)
    return out


def _status(data):
    if data["timers_stale"] > 0:
        return "alarm"
    for backend in ("restic", "borg"):
        age = data[backend].get("age_hours")
        if age is not None and age > ALARM_AGE_HOURS:
            return "alarm"
    if data["configured"] == 0 and data["backup_timers"] == 0:
        return "none"
    return "ok"


def discover():
    conf = read_conf()
    restic = _collect_restdic(conf)
    borg = _collect_borg(conf)
    data = {
        "status": "none",
        "configured": 1 if conf.get("restic_repository") or conf.get("borg_repository") else 0,
        "restic": restic,
        "borg": borg,
    }
    data.update(collect_timers())
    data["status"] = _status(data)
    return data


def metrics_payload(data):
    payload = {
        "configured": data.get("configured", 0),
        "backup_timers": data.get("backup_timers", 0),
        "timers_stale": data.get("timers_stale", 0),
    }
    for backend in ("restic", "borg"):
        rec = data.get(backend) or {}
        if rec.get("snapshots") is not None:
            payload[backend + "_snapshots"] = int(rec["snapshots"])
        if rec.get("age_hours") is not None:
            payload[backend + "_last_age_hours"] = float(rec["age_hours"])
    return {k: v for k, v in payload.items() if v is not None}


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def fail(message):
    sys.stderr.write("TA-backup collection error: %s\n" % message)
    return 1
