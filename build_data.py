#!/usr/bin/env python3
"""Génère data/network.json (tracés réels OSM + trains théoriques du jour) et data/incidents.json.

  python build_data.py --auto        # reconstruit les trains à 0h/6h/12h/18h (Paris), sinon incidents seulement
  python build_data.py --force       # tout reconstruire
Options : --gtfs URL|fichier  --date AAAA-MM-JJ  --types 0,1,2,5,7  --no-osm  --only-incidents
Variable PRIM_API_KEY : jeton PRIM (incidents). Tout est gratuit : GTFS IDFM, OpenStreetMap (Overpass), PRIM.
"""
import argparse, hashlib, io, json, math, os, re, sys, time, urllib.parse, urllib.request, zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

GTFS_URL = "https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip"
DISRUPTIONS_URL = "https://prim.iledefrance-mobilites.fr/marketplace/disruptions_bulk/disruptions/v2"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
BBOX = (48.10, 1.40, 49.25, 3.60)  # sud, ouest, nord, est
KINDS = {"0": {"tram", "light_rail"}, "1": {"subway", "light_rail"}, "2": {"rail", "light_rail"}}
K, M, PARIS = math.cos(math.radians(48.85)), 111320.0, ZoneInfo("Europe/Paris")

def log(*a): print(*a, file=sys.stderr)
def dist_m(a, b): return math.hypot((a[0] - b[0]) * M, (a[1] - b[1]) * M * K)
def secs(s): h, m, x = s.split(":"); return int(h) * 3600 + int(m) * 60 + int(x)

def open_zip(src):
    if src.startswith("http"):
        log("Téléchargement", src)
        src = io.BytesIO(urllib.request.urlopen(src, timeout=300).read())
    return zipfile.ZipFile(src)

def table(z, name):
    if name not in z.namelist(): return None
    with z.open(name) as f: return pd.read_csv(f, dtype=str)

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

# ---------- voies réelles : OpenStreetMap ----------
def fetch_osm(cache, max_age=30):
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < max_age * 86400:
        return json.load(open(cache, encoding="utf-8"))
    s, w, n, e = BBOX
    ways = {}
    for i in range(3):
        for j in range(3):
            bb = (s + (n - s) * i / 3, w + (e - w) * j / 3, s + (n - s) * (i + 1) / 3, w + (e - w) * (j + 1) / 3)
            q = ('[out:json][timeout:180];way["railway"~"^(rail|subway|light_rail|tram)$"]'
                 '["service"!~"yard|siding|spur|crossover"](%f,%f,%f,%f);out geom;' % bb)
            for k in range(6):  # réessaie, en alternant les deux serveurs Overpass
                try:
                    req = urllib.request.Request(OVERPASS[k % 2], data=urllib.parse.urlencode({"data": q}).encode(),
                                                 headers={"User-Agent": "carte-trains-idf"})
                    res = json.load(urllib.request.urlopen(req, timeout=240))
                    for el in res["elements"]:
                        if el.get("type") == "way" and "geometry" in el:
                            ways[el["id"]] = {"r": el["tags"]["railway"], "n": el["nodes"],
                                              "g": [[p["lat"], p["lon"]] for p in el["geometry"]]}
                    break
                except Exception as ex:
                    log("Overpass :", ex); time.sleep(20 * (k + 1))
            else:
                raise RuntimeError("Overpass indisponible")
    data = list(ways.values())
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    json.dump(data, open(cache, "w", encoding="utf-8"), separators=(",", ":"))
    return data

class Net:
    """Graphe des voies d'un type (rail, métro, tram) : plus court chemin entre deux arrêts, le long des rails."""
    def __init__(self, ways, kinds):
        from scipy.sparse import coo_matrix
        from scipy.spatial import cKDTree
        ids, xy, r, c, w = {}, [], [], [], []
        for way in ways:
            if way["r"] not in kinds: continue
            ix = []
            for n, p in zip(way["n"], way["g"]):
                if n not in ids: ids[n] = len(xy); xy.append(p)
                ix.append(ids[n])
            for a, b in zip(ix, ix[1:]):
                if a != b:
                    d = dist_m(xy[a], xy[b]); r += [a, b]; c += [b, a]; w += [d, d]
        self.ok = len(xy) > 1
        if self.ok:
            self.xy = np.array(xy)
            self.g = coo_matrix((w, (r, c)), shape=(len(xy), len(xy))).tocsr()
            self.tree = cKDTree(self.xy * [1, K])

    def path(self, A, B):
        from scipy.sparse.csgraph import dijkstra
        if not self.ok: return None
        da, ia = self.tree.query([A[0], A[1] * K]); db, ib = self.tree.query([B[0], B[1] * K])
        if da * M > 200 or db * M > 200: return None  # arrêt trop loin de toute voie
        d, pr = dijkstra(self.g, indices=ia, return_predecessors=True, limit=max(3 * dist_m(A, B), 600))
        if not np.isfinite(d[ib]): return None
        p = [ib]
        while p[-1] != ia: p.append(pr[p[-1]])
        return [tuple(self.xy[i]) for i in reversed(p)]

def slice_shape(pts, A, B):  # repli : portion du tracé GTFS entre deux arrêts
    arr = np.array(pts)
    ia = int(((arr[:, 0] - A[0]) ** 2 + ((arr[:, 1] - A[1]) * K) ** 2).argmin())
    sub = arr[ia:]
    ib = ia + int(((sub[:, 0] - B[0]) ** 2 + ((sub[:, 1] - B[1]) * K) ** 2).argmin())
    seg = [tuple(p) for p in arr[ia:ib + 1]]
    ok = len(seg) > 1 and sum(dist_m(x, y) for x, y in zip(seg, seg[1:])) < 2 * dist_m(A, B) + 300
    return seg if ok else None

# ---------- réseau ----------
def build_network(a, out, slot):
    now = datetime.now(PARIS)
    day = datetime.fromisoformat(a.date).date() if a.date else now.date()
    z = open_zip(a.gtfs)
    types = set(a.types.split(","))
    routes = table(z, "routes.txt")
    routes = routes[routes.route_type.isin(types)]
    rmeta = {r.route_id: (r.route_short_name, getattr(r, "route_color", None), r.route_type) for r in routes.itertuples()}
    trips = table(z, "trips.txt")
    trips = trips[trips.route_id.isin(rmeta)]
    shp = trips["shape_id"] if "shape_id" in trips else pd.Series([np.nan] * len(trips), index=trips.index)
    today = set(trips[trips.service_id.isin(active_services(z, day))].trip_id)
    yest = set(trips[trips.service_id.isin(active_services(z, day - timedelta(1)))].trip_id)  # courses d'hier après minuit
    # Géométrie : TOUTES les courses du GTFS (toutes les lignes sont dessinées) ; horaires : seulement aujourd'hui
    tinfo = {t: (rid, s if isinstance(s, str) else "") for t, rid, s in zip(trips.trip_id, trips.route_id, shp)}
    log(len(tinfo), "courses au total,", len(today), "le", day)
    stops = table(z, "stops.txt").set_index("stop_id")[["stop_lat", "stop_lon"]].astype(float)
    sx = dict(zip(stops.index, zip(stops.stop_lat, stops.stop_lon)))
    ids, valid, parts = set(tinfo), set(sx), []
    with z.open("stop_times.txt") as f:
        for ch in pd.read_csv(f, dtype=str, chunksize=500_000,
                              usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]):
            parts.append(ch[ch.trip_id.isin(ids) & ch.stop_id.isin(valid)])
    st = pd.concat(parts)
    st["seq"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "seq"], kind="stable")
    st["t"] = st.departure_time.fillna(st.arrival_time)
    T, S, TT = st.trip_id.to_numpy(), st.stop_id.to_numpy(), st.t.to_numpy()
    cut = np.flatnonzero(T[1:] != T[:-1]) + 1
    starts, ends = np.r_[0, cut], np.r_[cut, len(T)]
    raw, sh = {}, table(z, "shapes.txt")
    if sh is not None:
        sh = sh[sh.shape_id.isin(set(shp.dropna()))].copy()
        sh["seq"] = sh.shape_pt_sequence.astype(int)
        for sid, g in sh.sort_values(["shape_id", "seq"]).groupby("shape_id"):
            raw[sid] = list(zip(g.shape_pt_lat.astype(float), g.shape_pt_lon.astype(float)))
    nets = {}
    if not a.no_osm:
        try:
            ways = fetch_osm(a.osm_cache)
            nets = {t: Net(ways, KINDS[t]) for t in types if t in KINDS}
            log("Voies OSM chargées :", len(ways), "tronçons")
        except Exception as ex:
            log("OSM indisponible, repli sur les tracés GTFS :", ex)
    segs, sidx, osm_fail = [], {}, set()

    def seg_for(rt, A, B, sid):
        k, pa, pb, pts = (rt, A, B), sx[A], sx[B], None
        if k in sidx: return sidx[k]
        if rt in nets and k not in osm_fail:
            pts = nets[rt].path(pa, pb)
            if not pts: osm_fail.add(k)
        if not pts:
            k = (rt, A, B, sid)
            if k in sidx: return sidx[k]
            if sid in raw: pts = slice_shape(raw[sid], pa, pb)
        if not pts: pts = [pa, pb]  # dernier recours : ligne droite
        segs.append([[round(p[0], 5), round(p[1], 5)] for p in pts])
        sidx[k] = len(segs) - 1
        return sidx[k]

    patterns, pidx, rlist, ridx, tr = [], {}, [], {}, []
    for s0, e1 in zip(starts, ends):
        if e1 - s0 < 2: continue
        tid = T[s0]; rid, sid = tinfo[tid]; sids = tuple(S[s0:e1]); shifts, ts = [], None
        if tid in today or tid in yest:
            try:
                ts = [secs(x) for x in TT[s0:e1]]
                shifts = ([0] if tid in today else []) + ([-86400] if tid in yest and ts[-1] >= 86400 else [])
            except Exception:
                ts = None
        name, col, rt = rmeta[rid]
        if rid not in ridx:
            ridx[rid] = len(rlist)
            rlist.append({"id": rid.split(":")[-1], "n": name, "c": "#" + col if isinstance(col, str) and col else "#888888",
                          "t": int(rt), "g": set()})
        key = (rt, sids, sid)
        if key not in pidx:
            pidx[key] = len(patterns)
            patterns.append([seg_for(rt, x, y, sid) for x, y in zip(sids, sids[1:])])
            rlist[ridx[rid]]["g"].update(patterns[-1])
        for s in shifts:
            tr.append([ridx[rid], pidx[key], ts[0] + s, [max(0, y - x) for x, y in zip(ts, ts[1:])]])
    for r in rlist: r["g"] = sorted(r["g"])
    path = os.path.join(out, "network.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": day.isoformat(), "slot": slot, "routes": rlist, "segs": segs, "patterns": patterns, "trips": tr},
                  f, ensure_ascii=False, separators=(",", ":"))
    log(f"{len(rlist)} lignes, {len(tr)} trains aujourd'hui, {len(segs)} tronçons, {os.path.getsize(path)/1e6:.1f} Mo")

# ---------- incidents ----------
def build_incidents(out):
    key, items = os.environ.get("PRIM_API_KEY"), []
    if not key:
        log("PRIM_API_KEY absente : incidents ignorés"); return
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
        log("Incidents indisponibles (on garde l'ancien fichier) :", e); return
    path = os.path.join(out, "incidents.json")
    try: old = json.load(open(path, encoding="utf-8")).get("items")
    except Exception: old = None
    if old != items:  # n'écrit que si quelque chose a changé : pas de commit inutile toutes les 20 min
        json.dump({"updated": datetime.now(PARIS).isoformat(timespec="minutes"), "items": items},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    log(len(items), "incidents")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=GTFS_URL); ap.add_argument("--date"); ap.add_argument("--types", default="0,1,2,5,7")
    ap.add_argument("--out", default="data"); ap.add_argument("--osm-cache", default="cache/osm_rail.json")
    ap.add_argument("--no-osm", action="store_true"); ap.add_argument("--only-incidents", action="store_true")
    ap.add_argument("--auto", action="store_true"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    now = datetime.now(PARIS); slot = f"{now.date()}-{now.hour // 6}"  # 4 tranches : 0h 6h 12h 18h
    rebuild = not a.only_incidents
    if a.auto and not a.force:
        try: rebuild = json.load(open(os.path.join(a.out, "network.json")))["slot"] != slot
        except Exception: rebuild = True
    if rebuild: build_network(a, a.out, slot)
    else: log("Trains à jour pour la tranche", slot)
    build_incidents(a.out)
