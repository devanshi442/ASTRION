"""
AstroAgent Phase 0 probe.

Goal: answer gates G1-G3 with real data, before any pipeline exists.
  1. Horizons: predicted RA/Dec (+ geometry) for a comet at an observatory (I41 = Palomar/ZTF).
  2. IRSA ZTF image server: which science images cover that position and time.
  3. Download small cutouts, validate headers, project the predicted position to pixels.
  4. Save overlay PNGs, a provenance manifest (JSONL), and quarantine failures.

Everything marked [VERIFY] was NOT confirmed against live services when this was written.
The script prints column names and raw responses instead of guessing, and stops loudly on mismatch.

Usage (PowerShell, from E:\\astroagent\\repo):
  python probe.py --comet "C/2020 F3" --start 2020-07-12 --stop 2020-07-16 --step 1d --max-images 8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS

# ---------------------------------------------------------------- configuration
DATA_ROOT = Path(os.environ.get("ASTRO_DATA_ROOT", Path(__file__).resolve().parent / "data"))
DIRS = {n: DATA_ROOT / n for n in ("cache", "cutouts", "quarantine", "manifests", "figures")}

# Documented in IRSA's ZTF API page (verified).
IRSA_SCI_SEARCH = "https://irsa.ipac.caltech.edu/ibe/search/ztf/products/sci"
IRSA_DATA_ROOT = "https://irsa.ipac.caltech.edu/ibe/data/ztf/products/sci"

# Cutout parameter syntax: documented page confirms "center and size" plus gzip=false,
# but the exact string format was NOT visible when this was written. [VERIFY at
# https://irsa.ipac.caltech.edu/ibe/cutouts.html]. Size is assumed to default to degrees.
CUTOUT_SIZE_DEG = 0.05  # 3 arcmin

# Column names used to build science-image paths. Taken from the documented example filename
# ztf_20180411467847_000535_zr_c11_o_q3_sciimg.fits  [VERIFY the column names in the printed schema]
PATH_COLS = ["filefracday", "field", "ccdid", "qid", "filtercode", "imgtypecode"]

TIME_KEYWORDS = ["OBSJD", "MJDOBS", "DATE-OBS"]  # [VERIFY which one ZTF headers carry]
PIPELINE_VERSION = "probe-0.1"
USER_AGENT = "astroagent-probe/0.1 (student research; contact: set-your-email)"
MIN_INTERVAL_S = 1.0  # polite spacing between requests


# ---------------------------------------------------------------- helpers
def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_last_call = 0.0


def http_get(url: str, params: dict | None = None, retries: int = 4, timeout: int = 120) -> requests.Response:
    """GET with spacing, exponential backoff, and clear handling of auth/client errors."""
    global _last_call
    delay = 2.0
    for attempt in range(1, retries + 1):
        wait = MIN_INTERVAL_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
        except requests.RequestException as e:
            print(f"  [net error {attempt}/{retries}] {e}")
        else:
            if r.status_code in (401, 403):
                sys.exit(f"HTTP {r.status_code} from {url}\n"
                         "This looks like PROPRIETARY data (login required). Gate G1 is in question "
                         "for this date range: try an earlier/public range or a different survey.")
            if r.status_code == 400:
                return r  # IRSA returns 400 when a cutout does not intersect the image
            if r.status_code < 400:
                return r
            print(f"  [HTTP {r.status_code}] attempt {attempt}/{retries}")
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"giving up on {url}")


def cached_get(url: str, params: dict | None = None) -> bytes:
    """Disk cache keyed by URL+params; the raw response is kept for reproducibility."""
    key = sha256_bytes(json.dumps([url, params], sort_keys=True).encode())
    path = DIRS["cache"] / f"{key}.bin"
    if path.exists():
        return path.read_bytes()
    r = http_get(url, params)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {r.url}")
    path.write_bytes(r.content)
    (DIRS["cache"] / f"{key}.json").write_text(
        json.dumps({"url": r.url, "retrieved": utcnow(), "sha256": sha256_bytes(r.content)}, indent=1))
    return r.content


def quarantine(path: Path, reason: str) -> None:
    dest = DIRS["quarantine"] / path.name
    path.replace(dest)
    (DIRS["quarantine"] / (path.name + ".reason.txt")).write_text(reason)
    print(f"  QUARANTINED {path.name}: {reason}")


# ---------------------------------------------------------------- step 1: Horizons
def horizons_positions(comet: str, start: str, stop: str, step: str, location: str = "I41") -> Table:
    """Predicted astrometric RA/Dec and geometry. Uses astroquery.jplhorizons (wraps the documented
    Horizons API at https://ssd.jpl.nasa.gov/api/horizons.api)."""
    from astroquery.jplhorizons import Horizons

    obj = Horizons(id=comet, id_type="designation", location=location,
                   epochs={"start": start, "stop": stop, "step": step})
    # closest_apparition picks the apparition nearest the epochs for periodic comets. [VERIFY arg names
    # in your installed astroquery version]
    eph = obj.ephemerides(closest_apparition=True)
    print("Horizons columns:", eph.colnames)
    # Quantity 27 (sun-target and velocity position angles) is expected as 'sunTargetPA'/'velocityPA'.
    # [VERIFY] It is a PRIOR for tail direction, never a label.
    return eph


# ---------------------------------------------------------------- step 2: IRSA metadata
def ztf_metadata(ra: float, dec: float) -> Table:
    """Documented: GET .../products/sci?POS=ra,dec  (IPAC-table response)."""
    raw = cached_get(IRSA_SCI_SEARCH, {"POS": f"{ra:.6f},{dec:.6f}", "INTERSECT": "CENTER"})
    # INTERSECT=CENTER is shown in IRSA's curl example (with a POS table upload); using it as a GET
    # parameter is [VERIFY].
    tmp = DIRS["cache"] / "last_meta.tbl"
    tmp.write_bytes(raw)
    tbl = Table.read(str(tmp), format="ipac")
    return tbl


def sci_path(row) -> str:
    ffd = str(int(row["filefracday"]))
    return (f"{IRSA_DATA_ROOT}/{ffd[:4]}/{ffd[4:8]}/{ffd[8:]}/"
            f"ztf_{ffd}_{int(row['field']):06d}_{row['filtercode']}_c{int(row['ccdid']):02d}_"
            f"{row['imgtypecode']}_q{int(row['qid'])}_sciimg.fits")


# ---------------------------------------------------------------- step 3: cutout + validation
def download_cutout(row, ra: float, dec: float, name: str) -> Path | None:
    dest = DIRS["cutouts"] / name
    if dest.exists():
        return dest  # idempotent
    url = sci_path(row)
    params = {"center": f"{ra:.6f},{dec:.6f}", "size": CUTOUT_SIZE_DEG, "gzip": "false"}  # [VERIFY syntax]
    r = http_get(url, params)
    if r.status_code == 400:
        print(f"  no intersection / bad request for {name}")
        return None
    if r.status_code != 200 or len(r.content) < 2880:
        print(f"  unexpected response for {name}: {r.status_code}, {len(r.content)} bytes")
        return None
    dest.write_bytes(r.content)
    return dest


def validate_and_project(path: Path, ra: float, dec: float) -> dict:
    """Return validation results; raises ValueError for quarantine-worthy files."""
    with fits.open(path) as hdul:
        hdu = next((h for h in hdul if h.data is not None and h.data.ndim == 2), None)
        if hdu is None:
            raise ValueError("no 2-D image HDU")
        hdr, data = hdu.header, np.asarray(hdu.data, dtype=float)
    if data.size == 0 or not np.isfinite(data).any():
        raise ValueError("empty or all-NaN image")
    wcs = WCS(hdr)
    if not wcs.has_celestial:
        raise ValueError("no celestial WCS")
    tkey = next((k for k in TIME_KEYWORDS if k in hdr), None)
    if tkey is None:
        raise ValueError(f"no timestamp keyword among {TIME_KEYWORDS}")
    x, y = wcs.world_to_pixel_values(ra, dec)
    ny, nx = data.shape
    inside = bool(0 <= x < nx and 0 <= y < ny)
    return {"wcs_ok": True, "time_key": tkey, "time_value": str(hdr[tkey]), "bunit": hdr.get("BUNIT"),
            "filter": hdr.get("FILTER") or hdr.get("FILTERID"), "shape": [ny, nx],
            "pred_xy": [float(x), float(y)], "pred_inside": inside,
            "nan_frac": float(np.mean(~np.isfinite(data))), "data": data}


def save_overlay(data: np.ndarray, xy, out: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    lo, hi = np.nanpercentile(data, [5, 99.5])
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.imshow(data, origin="lower", cmap="gray", vmin=lo, vmax=hi)
    ax.plot(xy[0], xy[1], "o", mfc="none", mec="red", ms=22)
    ax.set_title(title, fontsize=8)
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comet", default="C/2020 F3", help="designation (example only; not checked for ZTF coverage)")
    ap.add_argument("--start", default="2020-07-12")
    ap.add_argument("--stop", default="2020-07-16")
    ap.add_argument("--step", default="1d")
    ap.add_argument("--max-images", type=int, default=8)
    ap.add_argument("--time-window-days", type=float, default=0.5)
    args = ap.parse_args()

    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    manifest = DIRS["manifests"] / "probe_manifest.jsonl"
    done = set()
    if manifest.exists():
        done = {json.loads(l)["record_id"] for l in manifest.read_text().splitlines() if l.strip()}

    eph = horizons_positions(args.comet, args.start, args.stop, args.step)
    n_ok = n_bad = 0
    for e in eph:
        ra, dec, jd = float(e["RA"]), float(e["DEC"]), float(e["datetime_jd"])
        meta = ztf_metadata(ra, dec)
        if n_ok == 0 and n_bad == 0:
            print("\nIRSA ZTF schema (read this, then fix PATH_COLS if needed):\n", meta.colnames)
        if len(meta) == 0:
            print(f"JD {jd:.3f}: no images cover ({ra:.4f}, {dec:.4f})")
            continue
        missing = [c for c in PATH_COLS + ["obsjd"] if c not in meta.colnames]
        if missing:
            sys.exit(f"Schema mismatch, columns not found: {missing}. Adjust PATH_COLS/obsjd above.")
        near = meta[np.abs(np.asarray(meta["obsjd"], dtype=float) - jd) < args.time_window_days]
        print(f"JD {jd:.3f}: {len(meta)} images cover the position, {len(near)} within the time window")
        for row in near:
            if n_ok + n_bad >= args.max_images:
                break
            record_id = f"ztf_{int(row['filefracday'])}_{int(row['field']):06d}_c{int(row['ccdid']):02d}_q{int(row['qid'])}_{args.comet.replace(' ', '').replace('/', '')}"
            if record_id in done:
                continue
            name = record_id + ".fits"
            path = download_cutout(row, ra, dec, name)
            if path is None:
                continue
            try:
                info = validate_and_project(path, ra, dec)
            except Exception as ex:  # noqa: BLE001
                quarantine(path, str(ex))
                n_bad += 1
                continue
            save_overlay(info.pop("data"), info["pred_xy"], DIRS["figures"] / (record_id + ".png"),
                         f"{args.comet} JD {jd:.3f}  pred_inside={info['pred_inside']}")
            rec = {"record_id": record_id, "source_archive": "IRSA", "source_dataset": "ZTF sci",
                   "original_uri": sci_path(row), "retrieval_timestamp": utcnow(),
                   "object_designation": args.comet, "ra_pred_deg": ra, "dec_pred_deg": dec,
                   "coordinate_frame": "ICRF (Horizons astrometric)", "observer_code": "I41",
                   "file_checksum": sha256_file(path), "pipeline_version": PIPELINE_VERSION,
                   "ephem_source": "JPL Horizons via astroquery", "validation": info,
                   "label_source": "ephemeris-predicted position (NOT human-verified)",
                   "human_confirmed_source_at_pred": None}  # fill by eye after viewing the figure
            with open(manifest, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            n_ok += 1
            print(f"  OK {name}  pred_inside={info['pred_inside']}")
    print(f"\nDone. valid={n_ok} quarantined={n_bad}. Now OPEN {DIRS['figures']} and check by eye "
          "whether a source sits at the red circle (gate G3).")


if __name__ == "__main__":
    main()
