import numpy as np
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings("ignore")

import orekit_jpype as orekit
orekit.initVM()
from orekit_jpype.pyhelpers import download_orekit_data_curdir, setup_orekit_curdir

from org.orekit.propagation.analytical.tle import TLE, TLEPropagator
from org.orekit.time import AbsoluteDate, TimeScalesFactory
from org.orekit.frames import FramesFactory

CLOUD_API_URL = "https://aura-backend-api-7s3j.onrender.com"

class Config:
    DURATION_HOURS = 24
    COARSE_STEP_MIN = 2
    FINE_WINDOW_MIN = 10
    FINE_STEP_SEC = 4
    HBR = 0.035              
    MAX_SECONDARIES = 3
    SIGMA_PRIMARY_KM = 0.5    
    SIGMA_SECONDARY_KM = 0.5    

cfg = Config()

def fetch_json(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        return res.json()
    except: return None

def load_iss_tle():
    data = fetch_json("https://tle.ivanstanojevic.me/api/tle/25544")
    if data and "line1" in data: return data["line1"], data["line2"]
    return ("1 25544U 98067A   24283.17644455  .00006183  00000+0  11976-3 0  9995", "2 25544  51.6332 347.1486 0007016 330.4977  29.5614 15.49124187592838")

def load_starlink_tles(max_count=3):
    data = fetch_json(f"https://tle.ivanstanojevic.me/api/tle?search=starlink&page=1&page_size={max_count}")
    tles = {}
    if data and "member" in data:
        for item in data["member"]:
            name = item.get("name", "UNKNOWN")
            if "STARLINK" in name.upper(): tles[name] = (item["line1"], item["line2"])
    if not tles:
        tles["STARLINK-1007 (FALLBACK)"] = ("1 44713U 19074A   24283.24401312  .00009188  00000+0  75180-4 0  9990", "2 44713  53.1447 132.8704 0001319  92.5375 267.5771 15.48919233385157")
        tles["STARLINK-1019 (FALLBACK)"] = ("1 44731U 19074U   24283.22275406  .00008365  00000+0  68397-4 0  9991", "2 44731  53.1440 134.9503 0001142  91.1855 268.9281 15.49377448385255")
    return tles

def create_propagator(line1, line2): return TLEPropagator.selectExtrapolator(TLE(line1, line2))

def get_pv(propagator, date):
    pv = propagator.getPVCoordinates(date, FramesFactory.getEME2000())
    return (np.array([pv.getPosition().getX(), pv.getPosition().getY(), pv.getPosition().getZ()]) / 1000.0,
            np.array([pv.getVelocity().getX(), pv.getVelocity().getY(), pv.getVelocity().getZ()]) / 1000.0)

def compute_rtn_primary(r_primary, v_primary, rel_pos):
    r_hat = r_primary / np.linalg.norm(r_primary)
    N_hat = np.cross(r_primary, v_primary)
    N_hat /= np.linalg.norm(N_hat)
    T_hat = np.cross(N_hat, r_hat)
    return (float(np.dot(rel_pos, r_hat)), float(np.dot(rel_pos, T_hat)), float(np.dot(rel_pos, N_hat)))

def pc_foster_exact(miss_2d, cov_2d, hbr, n_theta=60, n_r=40):
    xm, ym = miss_2d
    sxx, syy, sxy = cov_2d[0, 0], cov_2d[1, 1], cov_2d[0, 1]
    det = sxx * syy - sxy * sxy
    if det <= 0: return 0.0
    theta = np.linspace(0, 2 * np.pi, n_theta, endpoint=False)
    r = np.linspace(0, hbr, n_r)
    R, T = np.meshgrid(r, theta, indexing="ij")
    X, Y = R * np.cos(T), R * np.sin(T)
    dx, dy = X - xm, Y - ym
    quad = (syy * dx ** 2 - 2 * sxy * dx * dy + sxx * dy ** 2) / (2.0 * det)
    pdf = np.exp(-quad) / (2.0 * np.pi * np.sqrt(det))
    return float(np.clip(np.sum(pdf * R) * (r[1] - r[0]) * (theta[1] - theta[0]), 0.0, 1.0))

def analyze_one(primary, sec_tle, name, start):
    sec_tle_obj = TLE(sec_tle[0], sec_tle[1])
    prop = create_propagator(*sec_tle)
    
    best_dist, best_date = 1e9, start
    for m in range(0, cfg.DURATION_HOURS * 60, cfg.COARSE_STEP_MIN):
        date = start.shiftedBy(m * 60.0)
        d = np.linalg.norm(get_pv(primary, date)[0] - get_pv(prop, date)[0])
        if d < best_dist: best_dist, best_date = d, date

    min_dist, tca = 1e9, best_date
    for s in range(-cfg.FINE_WINDOW_MIN * 60, cfg.FINE_WINDOW_MIN * 60 + 1, cfg.FINE_STEP_SEC):
        date = best_date.shiftedBy(float(s))
        d = np.linalg.norm(get_pv(primary, date)[0] - get_pv(prop, date)[0])
        if d < min_dist: min_dist, tca = d, date

    p1, v1 = get_pv(primary, tca)
    p2, v2 = get_pv(prop, tca)
    rel_pos, rel_vel = p1 - p2, v1 - v2

    rtn = compute_rtn_primary(p1, v1, rel_pos)

    age_p = tca.durationFrom(primary.getTLE().getDate()) / 86400.0
    age_s = tca.durationFrom(sec_tle_obj.getDate()) / 86400.0
    
    s_p = cfg.SIGMA_PRIMARY_KM * (1.0 + max(0.1, age_p) / 2.0) ** 1.5
    s_s = cfg.SIGMA_SECONDARY_KM * (1.0 + max(0.1, age_s) / 2.0) ** 1.5
    cov_combined = np.eye(3) * (s_p**2) + np.eye(3) * (s_s**2)

    v_hat = rel_vel / (np.linalg.norm(rel_vel) + 1e-12)
    ref = np.array([0.0, 0.0, 1.0]) if abs(v_hat[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    e1 = np.cross(v_hat, ref); e1 /= np.linalg.norm(e1)
    e2 = np.cross(v_hat, e1)
    
    miss_2d = np.array([np.dot(rel_pos, e1), np.dot(rel_pos, e2)])
    P = np.vstack([e1, e2])
    cov_2d = P @ cov_combined @ P.T

    pc_exact = pc_foster_exact(miss_2d, cov_2d, cfg.HBR)
    risk = "RED" if pc_exact >= 1e-4 else "ORANGE" if pc_exact >= 1e-5 else "YELLOW" if pc_exact >= 1e-6 else "GREEN"

    return {
        "primary_object": "ISS (ZARYA)",
        "secondary_object": name,
        "tca": str(tca),
        "miss_distance_m": round(min_dist * 1000, 1),
        "relative_speed_km_s": round(float(np.linalg.norm(rel_vel)), 4),
        "pc_foster_exact": float(f"{pc_exact:.2e}"),
        "risk": risk,
        "rtn_data": {"radial": round(rtn[0]*1000, 1), "in_track": round(rtn[1]*1000, 1), "cross_track": round(rtn[2]*1000, 1)}
    }

if __name__ == "__main__":
    print("🧹 Clearing old cloud database...")
    requests.delete(f"{CLOUD_API_URL}/api/reports/clear")
    
    print("🚀 Engine started...")
    download_orekit_data_curdir()
    setup_orekit_curdir()
    
    utc = TimeScalesFactory.getUTC()
    now_dt = datetime.now(timezone.utc)
    start = AbsoluteDate(now_dt.year, now_dt.month, now_dt.day, now_dt.hour, now_dt.minute, float(now_dt.second), utc)
    
    iss_l1, iss_l2 = load_iss_tle()
    primary = create_propagator(iss_l1, iss_l2)
    secondaries = load_starlink_tles(cfg.MAX_SECONDARIES)

    results = []
    with ThreadPoolExecutor(max_workers=cfg.MAX_SECONDARIES) as executor:
        futures = {executor.submit(analyze_one, primary, tle, name, start): name for name, tle in secondaries.items()}
        for fut in as_completed(futures):
            results.append(fut.result())
            
    print(f"✅ Engine finished calculating {len(results)} objects.")
    
    print("☁️ Uploading results to Cloud Database...")
    for res in results:
        requests.post(f"{CLOUD_API_URL}/api/reports/", json=res)
            
    print("🎉 Automation Run Complete!")
