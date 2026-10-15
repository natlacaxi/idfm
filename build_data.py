#!/usr/bin/env python3
"""Génère data/network.json (lignes, tracés, trains théoriques du jour) et data/incidents.json.

Usage : python build_data.py [--gtfs URL_OU_FICHIER] [--date AAAA-MM-JJ] [--types 1,2] [--only-incidents]
types GTFS : 0 tram, 1 métro, 2 train/RER. PRIM_API_KEY (variable d'environnement) active les incidents.
"""
import argparse, hashlib, io, json, math, os, re, sys, urllib.request, zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

GTFS_URL = "https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip"
DISRUPTIONS_URL = "https://prim.iledefrance-mobilites.fr/marketplace/disruptions_bulk/disruptions/v2"
K = math.cos(math.radians(48.85))  # correction longitude (même métrique que la page)

def open_zip(src):
    if src.startswith("http"):
        print("Téléchargement", src, file=sys.stderr)
        src = io.BytesIO(urllib.request.urlopen(src, timeout=300).read())
    return zipfile.ZipFile(src)

def table(z, name):
    if name not in z.namelist():
        return None
    with z.open(name) as f:
        return pd.read_csv(f, dtype=str)

def secs(s):
    h, m, x = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(x)

def active_services(z, day):
    ymd, ok = day.strftime("%Y%m%d"), set()
    cal = table(z, "calendar.txt")
    if cal is not None:
        ok |= set(cal[(cal.start_date <= ymd) & (cal.end_date >= ymd) & (cal[day.strftime("%A").lower()] == "1")].service_id)
    cd = table(z, "calendar_dates.txt")
    if cd is not None:
        t = cd[cd.date == ymd]
        ok |= set(t[t.exception_type == "1"].service_id)
        ok -= set(t[t.exception_type == "2"].service_id)
    return ok

def thin(pts, step=20):  # supprime les points à moins de 20 m du précédent
    out = [pts[0]]
    for p in pts[1:-1]:
        if math.hypot((p[0] - out[-1][0]) * 111320, (p[1] - out[-1][1]) * 111320 * K) > step:
            out.append(p)
    out.append(pts[-1])
    return out

def cum_len(pts):
    c = [0.0]
    for a, b in zip(pts, pts[1:]):
        c.append(c[-1] + math.hypot(b[0] - a[0], (b[1] - a[1]) * K))
    return np.array(c)

def build_network(a, out):
    day = date.fromisoformat(a.date) if a.date else datetime.now(ZoneInfo("Europe/Paris")).date()
    z = open_zip(a.gtfs)
    routes = table(z, "routes.txt")
    routes = routes[routes.route_type.isin(set(a.types.split(",")))].set_index("route_id")
    trips = table(z, "trips.txt")
    trips = trips[trips.route_id.isin(routes.index) & trips.service_id.isin(active_services(z, day))]
    if "shape_id" not in trips:
        trips["shape_id"] = np.nan
    print(len(trips), "courses actives le", day, file=sys.stderr)
    stops = table(z, "stops.txt").set_index("stop_id")[["stop_lat", "stop_lon"]].astype(float)
    ids, parts = set(trips.trip_id), []
    with z.open("stop_times.txt") as f:
        for ch in pd.read_csv(f, dtype=str, chunksize=500_000,
                              usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]):
            parts.append(ch[ch.trip_id.isin(ids)])
    st = pd.concat(parts)
    st["seq"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "seq"])
    st["t"] = st.departure_time.fillna(st.arrival_time)
    raw = {}
    sh = table(z, "shapes.txt")
    if sh is not None:
        sh = sh[sh.shape_id.isin(set(trips.shape_id.dropna()))].copy()
        sh["seq"] = sh.shape_pt_sequence.astype(int)
        for sid, g in sh.sort_values(["shape_id", "seq"]).groupby("shape_id"):
            raw[sid] = thin(list(zip(g.shape_pt_lat.astype(float), g.shape_pt_lon.astype(float))))
    info = trips.set_index("trip_id")
    shapes, patterns, pidx, rlist, ridx, tr = {}, [], {}, [], {}, []
    for tid, g in st.groupby("trip_id", sort=False):
        t, sids = info.loc[tid], tuple(g.stop_id)
        if sum(s in stops.index for s in sids) < 2:
            continue
        try:
            times = [secs(x) for x in g.t]
        except Exception:
            continue
        sid = t.get("shape_id")
        if not isinstance(sid, str) or sid not in raw:  # pas de tracé : on relie les arrêts
            sid = "p:" + hashlib.md5("|".join(sids).encode()).hexdigest()[:10]
            raw.setdefault(sid, [tuple(stops.loc[s]) for s in sids if s in stops.index])
        key = (sid, sids)
        if key not in pidx:
            pts = raw[sid]
            c = cum_len(pts)
            tot, arr, prev, d = (c[-1] or 1), np.array(pts), 0, []
            for s in sids:
                if s not in stops.index:
                    d.append(d[-1] if d else 0)
                    continue
                la, lo = stops.loc[s]
                prev += int(((arr[prev:, 0] - la) ** 2 + ((arr[prev:, 1] - lo) * K) ** 2).argmin())
                d.append(int(round(c[prev] / tot * 10000)))  # position en dix-millièmes de la longueur du tracé
            shapes[sid] = [[round(p[0], 5), round(p[1], 5)] for p in pts]
            pidx[key] = len(patterns)
            patterns.append({"s": sid, "d": d})
        r = t.route_id
        if r not in ridx:
            m, col = routes.loc[r], routes.loc[r].get("route_color")
            ridx[r] = len(rlist)
            rlist.append({"id": r.split(":")[-1], "n": m.route_short_name,
                          "c": "#" + col if isinstance(col, str) and col else "#888888", "t": int(m.route_type), "s": {}})
        rs = rlist[ridx[r]]["s"]
        rs[sid] = rs.get(sid, 0) + 1
        tr.append([ridx[r], pidx[key], times])
    for r in rlist:
        r["s"] = [k for k, _ in sorted(r["s"].items(), key=lambda kv: -kv[1])[:6]]
    path = os.path.join(out, "network.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": day.isoformat(), "routes": rlist, "shapes": shapes, "patterns": patterns, "trips": tr},
                  f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(tr)} trains, {len(shapes)} tracés, {os.path.getsize(path)/1e6:.1f} Mo", file=sys.stderr)

def build_incidents(out):
    key, items = os.environ.get("PRIM_API_KEY"), []
    if not key:
        print("PRIM_API_KEY absente : incidents ignorés", file=sys.stderr)
    else:
        try:
            req = urllib.request.Request(DISRUPTIONS_URL, headers={"apikey": key})
            j = json.load(urllib.request.urlopen(req, timeout=60))
            dis, seen = {d["id"]: d for d in j.get("disruptions", [])}, set()
            for ln in j.get("lines", []):
                code = ln["id"].split(":")[-1]
                for io_ in ln.get("impactedObjects", []):
                    for did in io_.get("disruptionIds", []):
                        d = dis.get(did)
                        if d and (code, did) not in seen:
                            seen.add((code, did))
                            items.append({"line": code, "title": d.get("title", ""),
                                          "message": re.sub(r"<[^>]+>", " ", d.get("message") or "").strip()[:300],
                                          "severity": (d.get("severity") or {}).get("name", "")})
        except Exception as e:
            print("Incidents indisponibles :", e, file=sys.stderr)
    with open(os.path.join(out, "incidents.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="minutes"), "items": items},
                  f, ensure_ascii=False, separators=(",", ":"))
    print(len(items), "incidents", file=sys.stderr)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=GTFS_URL)
    ap.add_argument("--date")
    ap.add_argument("--types", default="1,2")
    ap.add_argument("--out", default="data")
    ap.add_argument("--only-incidents", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if not a.only_incidents:
        build_network(a, a.out)
    build_incidents(a.out)
