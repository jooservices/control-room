"""Collect REAL flow data (Jira, GitHub, the worker machine and its state store) into the documents the Control Room page shows.
Read-only: it never writes to any of those systems. Output: live_docs.json  {docName: {...}}"""
import collections, datetime, json, os, subprocess, sys, time, urllib.parse

SCRATCH = "/private/tmp/claude-501/-Users-vietvu-Workspaces/28814d22-6238-416d-9e52-5e0323aaa81d/scratchpad"
sys.path.insert(0, SCRATCH)
from j import call  # Jira REST through the Workspaces adapter

OUT = os.path.join(SCRATCH, "live")
HISTORY = os.path.join(OUT, "history.json")
NOW = datetime.datetime.now(datetime.timezone.utc)
KEYS = {"JAIF": "AI Flow", "JAVM": "JAVmeta", "JDTO": "DTO", "JOOC": "Client", "JOOCX": "CrawlerX", "JOODT": "Draytek", "JOOF": "Flickr",
        "JOONETM": "Network Management", "JOOS": "JOOservices", "JOOSX": "StoryX", "JOOUA": "Useragent", "JOOWT": "JOOservices WP Themes"}
REPOS = {"JAIF": "ai-flow", "JAVM": "javmeta", "JOOCX": "crawlerx", "JOOC": "client", "JDTO": "dto", "JOODT": "draytek", "JOOF": "flickr",
         "JOONETM": "network-management", "JOOSX": "storyx", "JOOUA": "useragent", "JOOWT": "jooservices-wp-themes", "JOOS": "Workspaces"}
BOARDS = {"JAIF": 191, "JAVM": 186, "JOOCX": 187}
FLOW_STATUSES = ["To Do", "In Progress", "In Review", "Review passed", "In QA", "Human review", "Ready to merge", "Blocked"]
ok = {}


def ts(s):
    return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)


def jts(s):
    """Jira timestamps look like 2026-10-19T21:40:29.855+0700."""
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(datetime.timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(jql, fields, expand=""):
    out, token = [], None
    while True:
        path = "/rest/api/3/search/jql?jql=" + urllib.parse.quote(jql) + "&maxResults=100&fields=" + fields + (("&expand=" + expand) if expand else "") + (f"&nextPageToken={token}" if token else "")
        status, body = call("GET", path)
        out += body.get("issues", [])
        token = body.get("nextPageToken")
        if not token or body.get("isLast"):
            return out


def sh(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout


# ---------------------------------------------------------------- WSL: worker machine and state store (read-only)
WSL_SCRIPT = r'''
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.expanduser("~/Workspaces/JOOservices/projects/ai-flow"))
from aiflow.config import load
from aiflow.lock import Redis
c = load(); r = Redis(c.redis_host, c.redis_port, c.redis_db, c.redis_prefix, c.redis_password, c.redis_user)
now = int(time.time()); out = {"now": now}
def sh(cmd):
    try: return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception: return ""
out["hb"] = {n: now - int(r.get(n) or 0) for n in r.scan("hb:*")}
out["leases"] = {n[6:]: r.get(n) for n in r.scan("lease:*")}
out["probe"] = {n[6:]: r.get(n) for n in r.scan("probe:*")}
out["cooldown"] = {n[9:]: True for n in r.scan("cooldown:*")}
out["paused"] = r.get("paused") is not None
out["ais"] = {n[4:]: r.get(n) for n in r.scan("ais:*")}
out["versions"] = {n[9:]: r.get(n) for n in r.scan("versions:*")}
out["roles"] = {k: r.get(k) for k in []}
usage = []
for n in r.scan("usage:*"):
    v = r.get(n)
    if v:
        try:
            d = json.loads(v); d["name"] = n; usage.append(d)
        except ValueError: pass
out["usage"] = usage
devs = {}
for n in r.scan("dev:*") + r.scan("reviewer:*"): devs[n] = r.get(n)
out["roleai"] = devs
out["qaslot"] = {n[7:]: r.get(n) for n in r.scan("qaslot:*")}
try:
    info = r.command("INFO", "stats"); mem = r.command("INFO", "memory"); ks = r.command("INFO", "keyspace")
    out["redis"] = {"ops": int([l for l in info.splitlines() if l.startswith("instantaneous_ops_per_sec")][0].split(":")[1]), "mem": [l.split(":")[1] for l in mem.splitlines() if l.startswith("used_memory_human")][0], "keys": ks.strip().replace("\r\n", " ")}
except Exception as e: out["redis"] = {"error": str(e)[:80]}
out["load"] = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
out["cpus"] = os.cpu_count()
mi = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
out["mem"] = {"total": round(mi["MemTotal"] / 1048576, 1), "avail": round(mi["MemAvailable"] / 1048576, 1)}
st = os.statvfs(os.path.expanduser("~")); out["disk"] = {"total": round(st.f_blocks * st.f_frsize / 1e9), "used": round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e9)}
out["uptime_h"] = round(float(open("/proc/uptime").read().split()[0]) / 3600, 1)
out["containers"] = len([l for l in sh("docker ps --format '{{.Names}}'").splitlines() if "workspaces-shared" in l])
out["services"] = {u: sh("systemctl --user is-active " + u) for u in ("aiflow-work@1", "aiflow-work@2", "aiflow-health.timer")}
out["code"] = sh("cd ~/Workspaces/JOOservices/projects/ai-flow && git rev-parse --short HEAD")
out["config"] = sh("cd ~/Workspaces/JOOservices/projects/ai-flow && python3 -m aiflow.supervisor check | head -30")
print(json.dumps(out))
'''


def wsl():
    try:
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "wsl", "python3 -I -"], input=WSL_SCRIPT, capture_output=True, text=True, timeout=120)
        data = json.loads(p.stdout.strip().splitlines()[-1])
        ok["wsl"] = True
        return data
    except Exception as e:
        ok["wsl"] = False
        print("WSL failed:", e, file=sys.stderr)
        return {}


# ---------------------------------------------------------------- GitHub (read-only)
def github():
    prs = {}
    for key, repo in REPOS.items():
        if repo == "Workspaces":
            continue
        out = sh(["gh", "pr", "list", "-R", "jooservices/" + repo, "--state", "all", "--limit", "40", "--json", "number,title,state,url,mergedAt,createdAt,headRefName,isDraft,author"])
        try:
            prs[key] = json.loads(out)
        except ValueError:
            prs[key] = []
    runs = []
    out = sh(["gh", "run", "list", "-R", "jooservices/ai-flow", "--limit", "30", "--json", "name,conclusion,status,createdAt,updatedAt,headBranch,url"])
    try:
        runs = json.loads(out)
    except ValueError:
        pass
    ok["github"] = bool(prs)
    return prs, runs


def main():
    docs = {}
    w = wsl()
    prs, runs = github()
    flow_projects = []
    cfg = w.get("config", "")
    for line in cfg.splitlines():
        if ":" in line and line.split(":")[0].strip() in KEYS and "repo" in line:
            flow_projects.append(line.split(":")[0].strip())
    if not flow_projects:
        flow_projects = ["JAIF"]

    # ---------------- Jira: every project
    projects, flow_issues = [], {}
    for k, name in KEYS.items():
        iss = fetch(f"project = {k} ORDER BY key ASC", "summary,status,issuetype,priority,labels,parent,created,updated,customfield_10213,customfield_10216", "changelog" if k in flow_projects else "")
        if k in flow_projects:
            flow_issues[k] = iss
        by = collections.Counter(i["fields"]["status"]["name"] for i in iss)
        nonepic = [i for i in iss if i["fields"]["issuetype"]["name"] != "Epic"]
        kids = collections.defaultdict(list)
        for i in nonepic:
            p = (i["fields"].get("parent") or {}).get("key")
            if p:
                kids[p].append(i["fields"]["status"]["name"])
        epics = [{"key": i["key"], "summary": i["fields"]["summary"][:90], "status": i["fields"]["status"]["name"], "children": len(kids.get(i["key"], [])), "done": sum(1 for s in kids.get(i["key"], []) if s == "Done")} for i in iss if i["fields"]["issuetype"]["name"] == "Epic"][:10]
        act = []
        for i in sorted(nonepic, key=lambda x: x["fields"]["updated"], reverse=True):
            s = i["fields"]["status"]["name"]
            if s in ("Done", "Declined"):
                continue
            act.append({"key": i["key"], "type": i["fields"]["issuetype"]["name"], "status": s, "priority": i["fields"]["priority"]["name"], "summary": i["fields"]["summary"][:100],
                        "parent": (i["fields"].get("parent") or {}).get("key"), "updated": i["fields"]["updated"][:16]})
        act = act[:25]
        recent = [{"key": i["key"], "summary": i["fields"]["summary"][:90], "closed": i["fields"]["updated"][:10]} for i in sorted(nonepic, key=lambda x: x["fields"]["updated"], reverse=True) if i["fields"]["status"]["name"] == "Done"][:5]
        day = sum(1 for i in nonepic if i["fields"]["status"]["name"] == "Done" and (NOW - ts(i["fields"]["updated"])).total_seconds() < 86400)
        merged = [{"number": p["number"], "title": p["title"][:90], "mergedAt": p["mergedAt"], "url": p["url"]} for p in prs.get(k, []) if p.get("mergedAt")][:4]
        projects.append({"key": k, "name": name, "repo": REPOS[k], "board": BOARDS.get(k), "total": len(iss), "counts": dict(by), "epics": epics, "active": act, "recent": recent,
                         "doneLast24h": day, "mergedPRs": merged, "openPRs": [{"number": p["number"], "title": p["title"][:80], "url": p["url"], "draft": p["isDraft"]} for p in prs.get(k, []) if p["state"] == "OPEN"][:5],
                         "lastUpdate": max((i["fields"]["updated"] for i in iss), default="")[:16], "onFlow": k in flow_projects})
    ok["jira"] = True
    docs["projects"] = {"list": projects}

    # ---------------- flow: tickets of the watched projects, from the Jira changelog
    usage = w.get("usage", [])
    ai_keys = {u["key"] for u in usage if "key" in u}  # tickets an AI really worked on (a ticket walked to Done by hand has none)
    transitions, cycles, reworks24 = [], [], 0
    all_flow = [i for lst in flow_issues.values() for i in lst]
    for i in all_flow:
        hist = sorted(i.get("changelog", {}).get("histories", []), key=lambda h: h["created"])
        first_start = None
        for h in hist:
            for it in h["items"]:
                if it["field"] == "status":
                    t = jts(h["created"])
                    transitions.append({"key": i["key"], "at": iso(t), "from": it["fromString"], "to": it["toString"], "summary": i["fields"]["summary"][:80]})
                    if it["toString"] == "In Progress" and first_start is None:
                        first_start = t
                    if it["toString"] == "Done" and first_start is not None and i["key"] in ai_keys and i["fields"]["issuetype"]["name"] != "Epic":
                        cycles.append((t - first_start).total_seconds() / 60)
                if it["field"] == "labels" and "rework" in (it.get("toString") or "") and "rework" not in (it.get("fromString") or ""):
                    if (NOW - jts(h["created"])).total_seconds() < 86400:
                        reworks24 += 1
    cycles.sort()

    def q(p):
        return round(cycles[min(len(cycles) - 1, int(len(cycles) * p))], 1) if cycles else None
    pr_by_key = {}
    for k2 in flow_projects:
        for p in prs.get(k2, []):
            for part in p["title"].split("(")[1:]:
                kk = part.split(")")[0]
                if kk.startswith(k2 + "-"):
                    pr_by_key[kk] = p
    inflight = []
    leases = w.get("leases", {})
    for i in all_flow:
        s = i["fields"]["status"]["name"]
        if s in FLOW_STATUSES and "aiflow-hold" not in i["fields"]["labels"]:
            pr = pr_by_key.get(i["key"])
            ag = i["fields"].get("customfield_10213")
            inflight.append({"key": i["key"], "project": i["key"].split("-")[0], "summary": i["fields"]["summary"][:100], "status": s, "priority": i["fields"]["priority"]["name"], "labels": i["fields"]["labels"], "agent": (ag or {}).get("value") if isinstance(ag, dict) else ag,
                             "updated": i["fields"]["updated"][:16], "lease": leases.get(i["key"]), "pr": ({"number": pr["number"], "url": pr["url"], "state": pr["state"], "draft": pr["isDraft"]} if pr else None),
                             "devAi": w.get("roleai", {}).get("dev:" + i["key"]), "reviewerAi": w.get("roleai", {}).get("reviewer:" + i["key"])})
    counts = collections.Counter(i["fields"]["status"]["name"] for i in all_flow if i["fields"]["issuetype"]["name"] != "Epic")
    done_recent = [i for i in all_flow if i["fields"]["status"]["name"] == "Done" and (NOW - ts(i["fields"]["updated"])).total_seconds() < 86400 and i["fields"]["issuetype"]["name"] != "Epic"]
    docs["flow"] = {"watched": flow_projects, "inflight": inflight, "statusCounts": dict(counts), "cycle": {"n": len(cycles), "p25": q(0.25), "p50": q(0.5), "p75": q(0.75)},
                    "doneLast24h": len(done_recent), "doneByAiLast24h": sum(1 for i in done_recent if i["key"] in ai_keys), "reworkLast24h": reworks24,
                    "mergedPRs24h": sum(1 for k2 in flow_projects for p in prs.get(k2, []) if p.get("mergedAt") and (NOW - ts(p["mergedAt"])).total_seconds() < 86400),
                    "qaslot": w.get("qaslot", {}), "paused": w.get("paused", False)}

    # ---------------- AI: configuration, probes and the usage log
    names = {"codex": ("Codex", "gpt-6-luna", "xhigh", "developer (default)"), "claude": ("Claude", "Sonnet", "default", "QA (default); developer for Highest priority"), "grok": ("Grok", "grok-build", "default", "reviewer (default)"), "opencode": ("OpenCode", "DeepSeek V4.1 Flash (free)", "default", "only when a ticket names it")}
    runs_by = collections.defaultdict(list)
    for u in usage:
        try:
            end = int(u["name"].rsplit(":", 1)[1])
        except Exception:
            continue
        u["end"] = end
        runs_by[u.get("ai")].append(u)
    ais = []
    for aid, (nm, model, eff, role) in names.items():
        rs = runs_by.get(nm, [])
        d24 = [r for r in rs if w.get("now", 0) - r["end"] < 86400]
        probe = w.get("probe", {}).get("wsl:" + nm) or ""
        ais.append({"id": aid, "name": nm, "model": model, "effort": eff, "roles": role, "probe": probe or "not probed yet", "usable": not probe.startswith("down"), "cooling": nm in w.get("cooldown", {}),
                    "runs24h": len(d24), "minutes24h": round(sum(r["minutes"] for r in d24), 1), "bad24h": sum(1 for r in d24 if r.get("why") != "ok"), "runsAll": len(rs), "minutesAll": round(sum(r["minutes"] for r in rs), 1), "badAll": sum(1 for r in rs if r.get("why") != "ok")})
    docs["ai"] = {"agents": ais, "versions": {k: json.loads(v) for k, v in w.get("versions", {}).items() if v}, "usable": w.get("ais", {})}

    # ---------------- timeline of real AI runs (last 6 hours) and real events
    base = w.get("now", int(time.time()))
    runs6 = []
    for u in usage:
        if "end" in u and base - u["end"] < 6 * 3600:
            runs6.append({"key": u["key"], "role": u["role"], "ai": u["ai"], "start": u["end"] - int(u["minutes"] * 60), "end": u["end"], "minutes": u["minutes"], "ok": u.get("why") == "ok"})
    runs6.sort(key=lambda r: r["start"])
    merges = [{"key": next((k for k, p2 in pr_by_key.items() if p2["number"] == p["number"] and p2["url"] == p["url"]), None), "number": p["number"], "title": p["title"][:80], "at": p["mergedAt"], "url": p["url"], "project": k2} for k2 in flow_projects for p in prs.get(k2, []) if p.get("mergedAt") and (NOW - ts(p["mergedAt"])).total_seconds() < 6 * 3600]
    docs["timeline"] = {"runs": runs6, "merges": merges, "now": base}
    events = []
    for t in transitions:
        if (NOW - ts(t["at"])).total_seconds() < 86400:
            events.append({"at": t["at"], "kind": "status", "key": t["key"], "text": f"{t['from']} → {t['to']}"})
    for u in usage:
        if "end" in u and base - u["end"] < 86400:
            events.append({"at": iso(datetime.datetime.fromtimestamp(u["end"], datetime.timezone.utc)), "kind": "run" if u.get("why") == "ok" else "badrun", "key": u["key"], "text": f"{u['ai']} finished a {u['role']} step in {u['minutes']} min" + ("" if u.get("why") == "ok" else f" (ended: {u.get('why')})")})
    for k2, lst in prs.items():
        for p in lst:
            if p.get("mergedAt") and (NOW - ts(p["mergedAt"])).total_seconds() < 86400:
                events.append({"at": p["mergedAt"], "kind": "merge", "key": None, "text": f"{REPOS[k2]} PR #{p['number']} merged: {p['title'][:70]}", "url": p["url"]})
    events.sort(key=lambda e: e["at"], reverse=True)
    docs["events"] = {"list": events[:80]}

    # ---------------- machine, with a rolling history kept locally
    hist = json.load(open(HISTORY)) if os.path.exists(HISTORY) else []
    if w:
        hist.append({"t": iso(NOW), "load": w["load"][0], "mem": round(w["mem"]["total"] - w["mem"]["avail"], 1)})
        hist = hist[-144:]
        json.dump(hist, open(HISTORY, "w"))
    busy = len([1 for k, v in leases.items()])
    docs["machine"] = {"host": "Workstation", "cpus": w.get("cpus"), "load": w.get("load"), "mem": w.get("mem"), "disk": w.get("disk"), "uptimeH": w.get("uptime_h"), "containers": w.get("containers"), "services": w.get("services"),
                       "heartbeatAges": w.get("hb"), "redis": w.get("redis"), "code": w.get("code"), "leases": leases, "workersBusy": busy, "history": hist,
                       "runs": [{"name": r["name"], "url": r["url"], "status": r["conclusion"] or r["status"], "mins": round((ts(r["updatedAt"]) - ts(r["createdAt"])).total_seconds() / 60, 1)} for r in runs[:8]]}
    docs["meta"] = {"asOf": iso(NOW), "ok": ok, "flowProjects": flow_projects}
    json.dump(docs, open(os.path.join(OUT, "live_docs.json"), "w"), separators=(",", ":"))
    print("ok", ok, "sizes", {k: len(json.dumps(v)) for k, v in docs.items()})


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    main()
