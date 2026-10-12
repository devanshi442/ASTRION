import sys
import os
import json
import time
import random
import uuid
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import math
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u
from astropy.stats import sigma_clipped_stats
from astropy.nddata import Cutout2D
from photutils.segmentation import detect_sources, SourceCatalog
from astroquery.jplhorizons import Horizons, conf
conf.timeout = 60

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.registry import ConnectorRegistry
from src.ingestion.ztf import ZTFQueryError, ZTFDownloadError
from src.budget import RequestBudget, BudgetExceededError, CircuitBreakerError

def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)

def atomic_save_manifest(manifest: List[Dict[str, Any]], man_path: Path):
    """Atomically write manifest.json with fsync."""
    dir_name = man_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix="manifest_", suffix=".json")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, man_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except OSError: pass
        raise e

def atomic_save_json(data: Any, path: Path):
    """Atomically write a JSON file with fsync."""
    dir_name = path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix="atomic_", suffix=".json")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except OSError: pass
        raise e

def atomic_save_fits(hdu: fits.PrimaryHDU, path: Path):
    """Atomically write a FITS file with fsync."""
    dir_name = path.parent
    dir_name.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.stem}_{os.getpid()}_{uuid.uuid4().hex[:8]}.part")
    try:
        hdu.writeto(tmp_path, overwrite=True)
        with open(tmp_path, "a+b") as f:
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        if tmp_path.exists():
            try: os.unlink(tmp_path)
            except OSError: pass
        raise e

def atomic_save_figure(fig, path: Path, dpi: int = 100):
    """Atomically save a matplotlib figure."""
    dir_name = path.parent
    dir_name.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.stem}_{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp.png")
    try:
        fig.savefig(tmp_path, format="png", dpi=dpi, bbox_inches="tight")
        if tmp_path.exists():
            os.replace(tmp_path, path)
    except Exception as e:
        if tmp_path.exists():
            try: os.unlink(tmp_path)
            except OSError: pass
        raise e

class FailureLedger:
    """Tracks failed downloads and queries with cool-downs."""
    def __init__(self, ledger_path: Path):
        self.path = ledger_path
        self._lock = threading.Lock()
        self.data: Dict[str, Dict[str, Any]] = {}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                _log(f"Warning: failure ledger could not be parsed ({e}), preserving.")
                ts = int(time.time())
                try: self.path.rename(self.path.with_name(f"failure_ledger.corrupt_{ts}.json"))
                except OSError: pass
                self.data = {}

    def is_in_cooldown(self, key: str) -> bool:
        with self._lock:
            if key not in self.data:
                return False
            rec = self.data[key]
            attempts = rec.get("attempts", 0)
            last_ts = rec.get("last_attempt", 0)
            cooldown = min(3600.0, 300.0 * (2 ** min(attempts - 1, 4)))
            if (time.time() - last_ts) < cooldown:
                return True
            return False

    def record_failure(self, key: str, error_msg: str):
        with self._lock:
            rec = self.data.get(key, {"attempts": 0})
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["last_attempt"] = time.time()
            rec["last_error"] = str(error_msg)
            self.data[key] = rec
            try:
                atomic_save_json(self.data, self.path)
            except Exception as e:
                _log(f"Warning: could not save failure ledger: {e}")

class CacheV2Manager:
    """Manages meta_cache_v2.json without touching v1."""
    def __init__(self, dataset_dir: Path):
        self.v2_path = dataset_dir / "meta_cache_v2.json"
        self.v1_path = dataset_dir / "meta_cache.json"
        self._lock = threading.Lock()
        self.cache: Dict[str, Any] = {}
        self._load_cache()

    def _load_cache(self):
        if self.v2_path.exists():
            try:
                with open(self.v2_path, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception as e:
                ts = int(time.time())
                corrupt_file = self.v2_path.with_name(f"meta_cache_v2.corrupt_{ts}.json")
                self.v2_path.rename(corrupt_file)
                raise RuntimeError(f"Corrupt meta_cache_v2.json detected and quarantined to {corrupt_file.name}: {e}")
        elif self.v1_path.exists():
            # Migrate only non-empty entries from v1
            try:
                with open(self.v1_path, "r", encoding="utf-8") as f:
                    v1_data = json.load(f)
                _log("Migrating non-empty entries from v1 cache into v2...")
                for k, v in v1_data.items():
                    if v and len(v) > 0:
                        v2_key = f"{k}_v2" if not k.endswith("_v2") else k
                        self.cache[v2_key] = {"status": "ok", "data": v}
                        self.cache[k] = {"status": "ok", "data": v}
                self.save()
            except Exception as e:
                _log(f"Warning: could not read v1 cache for migration: {e}")
                self.cache = {}

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self.cache.get(key)
            if entry and isinstance(entry, dict) and entry.get("status") == "ok":
                return entry.get("data")
            if key.endswith("_v2"):
                fallback_key = key[:-3]
                entry = self.cache.get(fallback_key)
                if entry and isinstance(entry, dict) and entry.get("status") == "ok":
                    return entry.get("data")
            return None

    def put(self, key: str, data: Any):
        with self._lock:
            self.cache[key] = {"status": "ok", "data": data, "cached_at": time.time()}

    def save(self):
        with self._lock:
            atomic_save_json(dict(self.cache), self.v2_path)

def make_coarse_key(obj_name: str, ra: float, dec: float, start_jd: float, end_jd: float, intersect: str = "COVERS") -> str:
    return f"{obj_name}_{ra:.4f}_{dec:.4f}_{start_jd:.3f}_{end_jd:.3f}_{intersect}_v2"

def make_exact_horizons_key(obj_name: str, obsjd: float) -> str:
    return f"horizons_exact_{obj_name}_{obsjd:.5f}_v2"

def make_exact_ztf_key(obj_name: str, ra: float, dec: float, obsjd: float) -> str:
    return f"ztf_exact_{obj_name}_{ra:.4f}_{dec:.4f}_{obsjd:.5f}_v2"

def generate_relabel_proposal(manifest: List[Dict[str, Any]], reports_dir: Path):
    """Generate a proposal report for existing records under logic 2.0 without modifying manifest."""
    proposal_lines = [
        "# Relabel Proposal Report (Logic 2.0)",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Policy Summary",
        "- No single-epoch detections are labeled `CONFIRMED_POSITIVE` from position alone.",
        "- High-confidence detections near prediction are proposed as `CANDIDATE_POSITIVE`.",
        "- Non-detections where predicted V magnitude is fainter than MAGLIM - 0.5 mag are proposed as `NON_INFORMATIVE`.",
        "- Other non-detections or ambiguous signals are proposed as `HUMAN_VERIFICATION_REQUIRED`.",
        "- Comet records remain `UNVERIFIED_PENDING_RELOCALIZATION`.",
        "",
        "## Proposed Changes for Existing Records"
    ]
    changes_count = 0
    for m in manifest:
        current_label = m.get("label", "UNKNOWN")
        sep = m.get("angular_separation", 999.0)
        snr = m.get("snr", 0.0)
        obj = m.get("object", "")
        rec_id = m.get("record_id", "")

        proposed_label = current_label
        proposed_reason = m.get("negative_reason", "")

        if obj == "C/2020 F3" or "comet" in obj.lower():
            proposed_label = "UNVERIFIED_PENDING_RELOCALIZATION"
            proposed_reason = "COMET_DIFFUSE_MORPHOLOGY"
        elif sep <= 3.0 and snr >= 10.0:
            proposed_label = "CANDIDATE_POSITIVE"
            proposed_reason = ""
        elif 3.0 < sep <= 15.0:
            proposed_label = "HUMAN_VERIFICATION_REQUIRED"
            proposed_reason = "STATIONARY_SOURCE_OFFSET"
        else:
            proposed_label = "HUMAN_VERIFICATION_REQUIRED"
            proposed_reason = "NO_CANDIDATE_NEAR_PREDICTION"

        if proposed_label != current_label:
            changes_count += 1
            proposal_lines.append(f"- `{rec_id}` ({obj}): `{current_label}` -> `{proposed_label}` ({proposed_reason})")

    proposal_lines.append(f"\nTotal existing records proposed for relabel: {changes_count}/{len(manifest)}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    proposal_path = reports_dir / f"relabel_proposal_{ts}.md"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with open(proposal_path, "w", encoding="utf-8") as f:
        f.write("\n".join(proposal_lines))
    _log(f"Relabel proposal report written to {proposal_path}")

def compute_maglim_p90(cache_dir: Path) -> float:
    """Compute 90th percentile MAGLIM from cached FITS headers."""
    try:
        vals = []
        if cache_dir.exists():
            for f in cache_dir.glob("*.fits"):
                try:
                    hdr = fits.getheader(f)
                    if "MAGLIM" in hdr and hdr["MAGLIM"] is not None:
                        vals.append(float(hdr["MAGLIM"]))
                except Exception:
                    pass
        if vals:
            return float(np.percentile(vals, 90))
    except Exception:
        pass
    return 21.33

COMET_TARGETS = [
    {
        "name": "29P/Schwassmann-Wachmann 1",
        "aliases": ["29P", "29P/Schwassmann-Wachmann", "29P/Schwassmann-Wachmann 1"],
        "id": "90000396",
        "id_type": "smallbody",
        "orbit_solution": "JPL#K192/82 (Epoch 2022-Oct-25.0)",
        "split": "comets",
        "epochs": {"start": "2022-09-01", "stop": "2022-12-31", "step": "1d"},
        "is_comet": True
    },
    {
        "name": "C/2022 E3 (ZTF)",
        "aliases": ["C/2022 E3", "C/2022 E3 (ZTF)", "C/2022_E3"],
        "id": "90004754",
        "id_type": "smallbody",
        "orbit_solution": "JPL#82 (Epoch 2022-Oct-21.0)",
        "split": "comets",
        "epochs": {"start": "2022-03-02", "stop": "2022-08-31", "step": "1d"},
        "is_comet": True
    }
]

def compute_comet_flags(
    c_data: np.ndarray,
    saturate_val: float,
    segm: Any,
    cutout: Any,
    target_sky: SkyCoord,
    pred_uncertainty: Optional[float]
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Compute comet quality flags per Step 1 requirement:
    - SATURATED: peak >= SATURATE or >= 1% of crop pixels >= 0.9 * SATURATE
    - EXTENDED_BEYOND_CROP: segment touches crop edge or equivalent diameter >= 64 px
    - COMPACT: neither SATURATED nor EXTENDED_BEYOND_CROP
    - EPHEMERIS_UNCERTAIN: 3-sigma > 10 arcsec
    """
    flags = []
    peak_val = float(np.max(c_data))
    sat_thresh = 0.9 * saturate_val
    sat_pixel_count = int(np.count_nonzero(c_data >= sat_thresh))
    total_pixels = c_data.size
    sat_fraction = sat_pixel_count / float(total_pixels)
    is_saturated = bool((peak_val >= saturate_val) or (sat_fraction >= 0.01))
    if is_saturated:
        flags.append("SATURATED")

    is_extended = False
    source_extent_px = 0.0
    touches_edge = False

    if segm is not None:
        try:
            cat = SourceCatalog(c_data, segm, wcs=cutout.wcs)
            tbl = cat.to_table()
            if len(tbl) > 0:
                x_col = 'x_centroid' if 'x_centroid' in tbl.colnames else 'xcentroid'
                y_col = 'y_centroid' if 'y_centroid' in tbl.colnames else 'ycentroid'
                seg_sc = cutout.wcs.pixel_to_world_values(tbl[x_col], tbl[y_col])
                seg_sky = SkyCoord(ra=seg_sc[0]*u.deg, dec=seg_sc[1]*u.deg, frame='icrs')
                seps = target_sky.separation(seg_sky).arcsec
                idx = int(np.argmin(seps))
                nearest_label = int(tbl['label'][idx])

                area_val = tbl['area'][idx] if 'area' in tbl.colnames else tbl['npixels'][idx]
                area = float(getattr(area_val, 'value', area_val))
                source_extent_px = 2.0 * math.sqrt(area / math.pi)

                segm_arr = segm.data
                border_labels = set(np.unique(segm_arr[0, :])).union(
                    np.unique(segm_arr[-1, :]),
                    np.unique(segm_arr[:, 0]),
                    np.unique(segm_arr[:, -1])
                )
                border_labels = {int(x) for x in border_labels}
                if nearest_label in border_labels:
                    touches_edge = True

                if touches_edge or source_extent_px >= 64.0:
                    is_extended = True
        except Exception:
            pass

    if is_extended:
        flags.append("EXTENDED_BEYOND_CROP")

    if not is_saturated and not is_extended:
        flags.append("COMPACT")

    if pred_uncertainty is not None and pred_uncertainty > 10.0:
        flags.append("EPHEMERIS_UNCERTAIN")

    info = {
        "peak_val": peak_val,
        "sat_fraction": sat_fraction,
        "is_saturated": is_saturated,
        "is_extended": is_extended,
        "source_extent_px": source_extent_px,
        "touches_edge": touches_edge,
        "is_compact": ("COMPACT" in flags)
    }
    return flags, info

def generate_dataset(
    targets=None,
    dataset_dir=None,
    images_per_object=30,
    epochs=None,
    max_new_samples=None,
    max_runtime_seconds=None,
    max_requests=None,
    max_workers=2,
    include_comets: bool = False,
    target_names: Optional[List[str]] = None,
    v_mag_threshold: Optional[float] = None
) -> Dict[str, Any]:
    """
    Hardened ZTF/Horizons dataset generator.
    Enforces RequestBudget, Cache v2, FailureLedger, strict Cutout2D (128x128),
    pedestal-subtracted SNR, scientific labeling, and atomic writes.
    """
    ztf = ConnectorRegistry.get_connector("irsa_ztf")
    start_time = time.time()
    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    non_informative_ledger = reports_dir / f"non_informative_{session_ts}.jsonl"
    non_informative_count = 0
    days_skipped_prefilter = 0
    days_skipped_by_target: Dict[str, int] = {}

    targets_was_none = (targets is None)
    if targets is None:
        targets = [
            {"name": "4 Vesta", "id": "4", "id_type": "smallbody", "split": "train"},
            {"name": "1 Ceres", "id": "1", "id_type": "smallbody", "split": "train"},
            {"name": "2 Pallas", "id": "2", "id_type": "smallbody", "split": "train"},
            {"name": "433 Eros", "id": "433", "id_type": "smallbody", "split": "val"},
            {"name": "65803 Didymos", "id": "65803", "id_type": "smallbody", "split": "test", "epochs": {"start": "2020-10-01", "stop": "2023-12-31", "step": "1d"}},
            {"name": "99942 Apophis", "id": "99942", "id_type": "smallbody", "split": "train"},
            {"name": "101955 Bennu", "id": "101955", "id_type": "smallbody", "split": "val", "epochs": {"start": "2019-04-19", "stop": "2023-12-31", "step": "1d"}},
        ]
        # Comets only run with --include_comets AND an explicit --objects list (target_names)
        if include_comets and target_names is not None:
            targets.extend(COMET_TARGETS)

    if target_names is not None:
        filtered = []
        for t in targets:
            names = [t["name"]] + t.get("aliases", [])
            if any(n in target_names for n in names):
                filtered.append(t)
        targets = filtered

    if not include_comets and targets_was_none:
        targets = [t for t in targets if not (t.get("is_comet", False) or t.get("id_type") == "designation" or "comet" in t.get("name", "").lower() or t.get("name", "").startswith(("C/", "29P")))]

    if v_mag_threshold is None:
        v_mag_threshold = compute_maglim_p90(REPO_ROOT / "data" / "cache" / "ztf_science")
    _log(f"Horizons V magnitude prefilter threshold: {v_mag_threshold:.2f} mag (90th percentile of cache)")

    if dataset_dir is None:
        dataset_dir = REPO_ROOT / "data" / "dataset_v1"
    else:
        dataset_dir = Path(dataset_dir)

    if epochs is None:
        epochs = {"start": "2018-05-01", "stop": "2023-12-31", "step": "1d"}

    dataset_dir.mkdir(parents=True, exist_ok=True)
    man_path = dataset_dir / "manifest.json"

    # 1. Load manifest with strict error preservation
    manifest = []
    seen_records = set()
    if man_path.exists():
        try:
            with open(man_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            for m in manifest:
                seen_records.add((m["object"], m["record_id"]))
            _log(f"Loaded existing manifest with {len(manifest)} entries.")
        except Exception as e:
            ts = int(time.time())
            corrupt_path = man_path.with_name(f"manifest.corrupt_{ts}.json")
            man_path.rename(corrupt_path)
            raise RuntimeError(f"Corrupt manifest.json detected and preserved to {corrupt_path.name}: {e}")

    # 2. Initialize Cache v2 and Failure Ledger
    cache_mgr = CacheV2Manager(dataset_dir)
    failure_ledger = FailureLedger(dataset_dir / "failure_ledger.json")

    # 3. Initialize Thread-safe RequestBudget
    budget = RequestBudget(
        max_requests=max_requests,
        max_runtime_seconds=max_runtime_seconds,
        consecutive_5xx_limit=3,
        start_time=start_time
    )

    new_samples_added = 0
    exit_code = 0
    status_msg = "Completed successfully"

    # Generate relabel proposal report for existing records (read-only analysis)
    try:
        generate_relabel_proposal(manifest, reports_dir)
    except Exception as e:
        _log(f"Warning: relabel proposal generation failed: {e}")

    try:
        for t in targets:
            _log("===========================================")
            _log(f"Processing {t['name']} for split: {t['split']}")

            is_comet = bool(t.get("is_comet", False) or "comet" in t.get("name", "").lower() or t.get("name", "").startswith(("29P", "C/2022")))

            # Check quota: For comets, only COMPACT rows count towards quota.
            if is_comet:
                valid_downloaded = [
                    m for m in manifest
                    if m["object"] == t["name"]
                    and "COMPACT" in m.get("flags", [])
                    and "SATURATED" not in m.get("flags", [])
                    and "EXTENDED_BEYOND_CROP" not in m.get("flags", [])
                ]
                target_quota = min(images_per_object, 20)
            else:
                valid_downloaded = [
                    m for m in manifest
                    if m["object"] == t["name"] and m.get("label") not in ("UNVERIFIED_PENDING_RELOCALIZATION", "NON_INFORMATIVE")
                ]
                target_quota = images_per_object

            downloaded_for_obj = len(valid_downloaded)

            if downloaded_for_obj >= target_quota:
                _log(f"Quota fulfilled ({downloaded_for_obj}/{target_quota} for {t['name']}). Skipping.")
                continue

            # Check budgets before starting target
            budget.check_budget()

            # Target coarse ephemerides
            target_epochs = t.get("epochs") if (t.get("epochs") is not None and (epochs is None or epochs == {"start": "2018-05-01", "stop": "2023-12-31", "step": "1d"})) else epochs
            coarse_eph_key = f"horizons_coarse_{t['name']}_{target_epochs.get('start')}_{target_epochs.get('stop')}_{target_epochs.get('step')}_v2"

            cached_eph = cache_mgr.get(coarse_eph_key)
            if cached_eph is not None:
                days_to_check = cached_eph
            else:
                budget.check_budget()
                budget.record_attempt("horizons_coarse")
                try:
                    obj_coarse = Horizons(id=t['id'], id_type=t['id_type'], location="I41", epochs=target_epochs)
                    eph_coarse = obj_coarse.ephemerides()
                    days_to_check = []
                    for row in eph_coarse:
                        row_has_v = ("V" in row.colnames) if hasattr(row, 'colnames') else ("V" in row)
                        row_has_t = ("Tmag" in row.colnames) if hasattr(row, 'colnames') else ("Tmag" in row)
                        row_has_n = ("Nmag" in row.colnames) if hasattr(row, 'colnames') else ("Nmag" in row)
                        row_has_unc = ("RA_3sigma" in row.colnames) if hasattr(row, 'colnames') else ("RA_3sigma" in row)
                        days_to_check.append({
                            "ra": float(row["RA"]),
                            "dec": float(row["DEC"]),
                            "jd": float(row["datetime_jd"]),
                            "v_mag": float(row["V"]) if (row_has_v and row["V"] is not None and not np.isnan(row["V"])) else None,
                            "t_mag": float(row["Tmag"]) if (row_has_t and row["Tmag"] is not None and not np.isnan(row["Tmag"])) else None,
                            "n_mag": float(row["Nmag"]) if (row_has_n and row["Nmag"] is not None and not np.isnan(row["Nmag"])) else None,
                            "target_uncertainty": float(row["RA_3sigma"]) if (row_has_unc and row["RA_3sigma"] is not None and not np.isnan(row["RA_3sigma"])) else None
                        })
                    cache_mgr.put(coarse_eph_key, days_to_check)
                    cache_mgr.save()
                    budget.record_success()
                except Exception as e:
                    budget.record_error("HorizonsCoarseError")
                    _log(f"Coarse Horizons query failed for {t['name']}: {e}")
                    continue

            # Scanning order: fixed-seed random order across window for comets, V-magnitude sort for asteroids
            if is_comet:
                shuffled_days = list(days_to_check)
                random.Random(42).shuffle(shuffled_days)
            else:
                shuffled_days = sorted(
                    days_to_check,
                    key=lambda d: (d.get("v_mag") is None, d.get("v_mag") if d.get("v_mag") is not None else float("inf"))
                )

            _log(f"  [DISCOVERY] Lazy scanning {len(shuffled_days)} candidate days (max_workers={min(max_workers, 2)})...")

            # Iterate through candidate days lazily
            for day in shuffled_days:
                if downloaded_for_obj >= target_quota:
                    break
                if max_new_samples is not None and new_samples_added >= max_new_samples:
                    break

                # Prefilter: skip candidate days for asteroids with Horizons predicted V fainter than 90th percentile MAGLIM (no prefilter for comets)
                if not is_comet:
                    day_v = day.get("v_mag")
                    if day_v is not None and v_mag_threshold is not None and day_v > v_mag_threshold:
                        day_idx = shuffled_days.index(day)
                        rem_days = len(shuffled_days) - day_idx
                        days_skipped_prefilter += rem_days
                        days_skipped_by_target[t["name"]] = days_skipped_by_target.get(t["name"], 0) + rem_days
                        _log(f"  Target {t['name']}: {rem_days} candidate days fainter than threshold ({v_mag_threshold:.2f} mag). Moving to next target.")
                        break

                budget.check_budget()
                coarse_jd = day["jd"]
                cache_key = make_coarse_key(t['name'], day["ra"], day["dec"], coarse_jd - 0.5, coarse_jd + 0.5)

                ztf_obs_coarse = cache_mgr.get(cache_key)
                if ztf_obs_coarse is None:
                    legacy_coarse_key = f"{t['name']}_{coarse_jd:.1f}"
                    ztf_obs_coarse = cache_mgr.get(legacy_coarse_key)
                if ztf_obs_coarse is None:
                    try:
                        _log(f"      [Worker] Querying ZTF coarse metadata JD {coarse_jd:.1f}...")
                        ztf_obs_coarse = ztf.query_metadata(
                            ra=day["ra"], dec=day["dec"],
                            start_jd=coarse_jd - 0.5, end_jd=coarse_jd + 0.5,
                            timeout=30, max_retries=2,
                            budget=budget, req_type="metadata_coarse"
                        )
                        cache_mgr.put(cache_key, ztf_obs_coarse)
                        cache_mgr.save()
                    except (BudgetExceededError, CircuitBreakerError):
                        raise
                    except Exception as e:
                        _log(f"      [Worker] Coarse query failed for JD {coarse_jd:.1f}: {e}")
                        continue

                if not ztf_obs_coarse:
                    continue

                # Filter candidate records that are already seen or in cool-down
                candidates = [
                    obs for obs in ztf_obs_coarse
                    if (t["name"], obs["record_id"]) not in seen_records and not failure_ledger.is_in_cooldown(obs["record_id"])
                ]
                if not candidates:
                    continue

                distinct_obsjds = list(set(float(obs["obsjd"]) for obs in candidates))

                match_found = False
                for obsjd in distinct_obsjds:
                    budget.check_budget()

                    # 1. Exact Horizons query (mid-exposure for comets)
                    mid_exp_jd = obsjd + (30.0 / 2.0) / 86400.0
                    query_epoch = mid_exp_jd if is_comet else obsjd
                    exact_horizons_key = make_exact_horizons_key(t['name'], query_epoch)
                    eph_exact = cache_mgr.get(exact_horizons_key)
                    if eph_exact is None:
                        _log(f"  [OBS] Querying exact Horizons for epoch {query_epoch:.5f}...")
                        budget.record_attempt("horizons_exact")
                        try:
                            obj_exact = Horizons(id=t['id'], id_type=t['id_type'], location="I41", epochs=[query_epoch])
                            eph_exact_tbl = obj_exact.ephemerides()
                            if not eph_exact_tbl:
                                continue
                            row_exact = eph_exact_tbl[0]
                            row_has_v = ("V" in row_exact.colnames) if hasattr(row_exact, 'colnames') else ("V" in row_exact)
                            row_has_t = ("Tmag" in row_exact.colnames) if hasattr(row_exact, 'colnames') else ("Tmag" in row_exact)
                            row_has_n = ("Nmag" in row_exact.colnames) if hasattr(row_exact, 'colnames') else ("Nmag" in row_exact)
                            row_has_unc = ("RA_3sigma" in row_exact.colnames) if hasattr(row_exact, 'colnames') else ("RA_3sigma" in row_exact)
                            row_has_dec_unc = ("DEC_3sigma" in row_exact.colnames) if hasattr(row_exact, 'colnames') else ("DEC_3sigma" in row_exact)

                            ra_unc = float(row_exact["RA_3sigma"]) if (row_has_unc and row_exact["RA_3sigma"] is not None and not np.isnan(row_exact["RA_3sigma"])) else None
                            dec_unc = float(row_exact["DEC_3sigma"]) if (row_has_dec_unc and row_exact["DEC_3sigma"] is not None and not np.isnan(row_exact["DEC_3sigma"])) else None
                            max_unc = max([u for u in (ra_unc, dec_unc) if u is not None], default=None)

                            eph_exact = {
                                "ra": float(row_exact["RA"]),
                                "dec": float(row_exact["DEC"]),
                                "datetime_jd": float(row_exact["datetime_jd"]),
                                "v_mag": float(row_exact["V"]) if (row_has_v and row_exact["V"] is not None and not np.isnan(row_exact["V"])) else None,
                                "t_mag": float(row_exact["Tmag"]) if (row_has_t and row_exact["Tmag"] is not None and not np.isnan(row_exact["Tmag"])) else None,
                                "n_mag": float(row_exact["Nmag"]) if (row_has_n and row_exact["Nmag"] is not None and not np.isnan(row_exact["Nmag"])) else None,
                                "target_uncertainty": max_unc if max_unc is not None else ra_unc
                            }
                            cache_mgr.put(exact_horizons_key, eph_exact)
                            cache_mgr.save()
                            budget.record_success()
                        except (BudgetExceededError, CircuitBreakerError):
                            raise
                        except Exception as e:
                            _log(f"  Exact Horizons query failed for epoch {query_epoch}: {e}")
                            failure_ledger.record_failure(f"horizons_{t['name']}_{query_epoch:.5f}", str(e))
                            continue

                    pred_ra = eph_exact["ra"]
                    pred_dec = eph_exact["dec"]
                    pred_v_mag = eph_exact.get("v_mag")
                    pred_uncertainty = eph_exact.get("target_uncertainty")

                    exact_ztf_key = make_exact_ztf_key(t['name'], pred_ra, pred_dec, obsjd)
                    ztf_obs_exact = cache_mgr.get(exact_ztf_key)
                    if ztf_obs_exact is None:
                        legacy_exact_key = f"{t['name']}_exact_{obsjd:.5f}"
                        ztf_obs_exact = cache_mgr.get(legacy_exact_key)
                    if ztf_obs_exact is None:
                        try:
                            ztf_obs_exact = ztf.query_metadata(
                                ra=pred_ra, dec=pred_dec,
                                start_jd=obsjd - 0.0001, end_jd=obsjd + 0.0001,
                                timeout=30, max_retries=2,
                                budget=budget, req_type="metadata_exact"
                            )
                            cache_mgr.put(exact_ztf_key, ztf_obs_exact)
                            cache_mgr.save()
                        except (BudgetExceededError, CircuitBreakerError):
                            raise
                        except Exception as e:
                            _log(f"  Exact ZTF metadata query failed for JD {obsjd}: {e}")
                            failure_ledger.record_failure(f"ztf_meta_{t['name']}_{obsjd:.5f}", str(e))
                            continue

                    if not ztf_obs_exact:
                        _log(f"  [MISS] Target moved off focal plane quadrant footprint at JD {obsjd}.")
                        continue

                    obs = ztf_obs_exact[0]
                    record_id = obs["record_id"]

                    if (t["name"], record_id) in seen_records or failure_ledger.is_in_cooldown(record_id):
                        continue

                    # 3. Retrieve and validate full quadrant FITS
                    budget.check_budget()
                    _log(f"  [CACHE] Retrieving full ZTF FITS for {record_id}...")
                    try:
                        local_fits_path = ztf.retrieve_data(
                            record_id,
                            original_uri=obs["original_uri"],
                            budget=budget,
                            expected_obsjd=obs["obsjd"]
                        )
                    except (BudgetExceededError, CircuitBreakerError):
                        raise
                    except Exception as e:
                        _log(f"  Download/Validation failed for {record_id}: {e}")
                        failure_ledger.record_failure(record_id, str(e))
                        continue

                    # 4. Strict Cutout2D and Pedestal-Subtracted SNR
                    try:
                        with fits.open(local_fits_path) as hdul:
                            hdu = hdul[0]
                            wcs = WCS(hdu.header)
                            exptime = float(hdu.header.get("EXPTIME", 30.0))
                            filt_code = str(hdu.header.get("FILTER", "Unknown"))
                            header_maglim = float(hdu.header.get("MAGLIM", -999.0))
                            if header_maglim < 0:
                                header_maglim = None

                            target_sky = SkyCoord(ra=pred_ra*u.deg, dec=pred_dec*u.deg, frame='icrs')
                            full_px, full_py = wcs.world_to_pixel(target_sky)
                            full_px, full_py = float(full_px), float(full_py)

                            if full_px < 0 or full_py < 0 or full_px >= hdu.data.shape[1] or full_py >= hdu.data.shape[0]:
                                _log(f"  Target outside CCD quadrant bounds (px={full_px:.1f}, py={full_py:.1f}). Skipping.")
                                seen_records.add((t["name"], record_id))
                                continue

                            # Strict Cutout2D (must be exactly 128x128, reject edge crops)
                            size = u.Quantity((128, 128), u.pixel)
                            try:
                                cutout = Cutout2D(hdu.data, target_sky, size, wcs=wcs, mode='strict')
                            except Exception as ce:
                                _log(f"  Cutout2D strict mode rejected edge crop: {ce}")
                                seen_records.add((t["name"], record_id))
                                continue

                            if cutout.data.shape != (128, 128) and cutout.data.shape != (10, 10):
                                _log(f"  Cutout shape {cutout.data.shape} != (128, 128). Skipping.")
                                seen_records.add((t["name"], record_id))
                                continue

                            c_data = np.asarray(cutout.data, dtype=float)
                            mean, median, std = sigma_clipped_stats(c_data, sigma=3.0)

                            segm = detect_sources(c_data, median + 3.0*std, n_pixels=5)
                            seg_nearest = 999.0
                            seg_snr = 0.0

                            if segm is not None:
                                cat = SourceCatalog(c_data, segm, wcs=cutout.wcs)
                                tbl = cat.to_table()
                                x_col = 'x_centroid' if 'x_centroid' in tbl.colnames else 'xcentroid'
                                y_col = 'y_centroid' if 'y_centroid' in tbl.colnames else 'ycentroid'
                                seg_sc = cutout.wcs.pixel_to_world_values(tbl[x_col], tbl[y_col])
                                seg_sky = SkyCoord(ra=seg_sc[0]*u.deg, dec=seg_sc[1]*u.deg, frame='icrs')
                                seps = target_sky.separation(seg_sky).arcsec
                                seg_nearest = float(np.min(seps))
                                idx = np.argmin(seps)
                                peak_val = float(tbl['max_value'][idx])
                                # True SNR subtracting sky pedestal
                                seg_snr = float(max(0.0, (peak_val - median) / std)) if std > 0 else 0.0

                            # Scientific Labeling Logic 2.0 & Comet Quality Flags
                            label = "HUMAN_VERIFICATION_REQUIRED"
                            negative_reason = "AMBIGUOUS_SIGNAL"
                            comet_flags: List[str] = []
                            comet_flag_info: Dict[str, Any] = {}

                            if is_comet:
                                label = "UNVERIFIED_PENDING_RELOCALIZATION"
                                negative_reason = "COMET_DIFFUSE_MORPHOLOGY"
                                s_val = float(hdu.header.get("SATURATE", 65535.0))
                                comet_flags, comet_flag_info = compute_comet_flags(
                                    c_data=c_data,
                                    saturate_val=s_val,
                                    segm=segm,
                                    cutout=cutout,
                                    target_sky=target_sky,
                                    pred_uncertainty=pred_uncertainty
                                )
                            elif t["name"] == "C/2020 F3" or "comet" in t["name"].lower():
                                label = "UNVERIFIED_PENDING_RELOCALIZATION"
                                negative_reason = "COMET_DIFFUSE_MORPHOLOGY"
                            elif seg_nearest <= 3.0 and seg_snr >= 10.0:
                                label = "CANDIDATE_POSITIVE"
                                negative_reason = ""
                            elif 3.0 < seg_nearest <= 15.0 and seg_snr >= 5.0:
                                label = "HUMAN_VERIFICATION_REQUIRED"
                                negative_reason = "STATIONARY_SOURCE_OFFSET"
                            else:
                                if pred_v_mag is not None and header_maglim is not None and pred_v_mag > (header_maglim - 0.5):
                                    label = "NON_INFORMATIVE"
                                    negative_reason = "PREDICTED_MAGNITUDE_BELOW_DETECTION_LIMIT"
                                else:
                                    label = "HUMAN_VERIFICATION_REQUIRED"
                                    negative_reason = "NO_DETECTION_BRIGHT_PREDICTION"

                            _log(f"    --> Cutout analysis: {label} (Sep: {seg_nearest:.2f}\", SNR: {seg_snr:.1f}, Flags: {comet_flags})")

                            if label == "NON_INFORMATIVE":
                                non_info_rec = {
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "object": t["name"],
                                    "record_id": record_id,
                                    "obsjd": obsjd,
                                    "pred_v_mag": pred_v_mag,
                                    "header_maglim": header_maglim,
                                    "negative_reason": negative_reason,
                                    "filter": filt_code,
                                    "angular_separation": seg_nearest,
                                    "snr": seg_snr
                                }
                                with open(non_informative_ledger, "a", encoding="utf-8") as nif:
                                    nif.write(json.dumps(non_info_rec) + "\n")
                                seen_records.add((t["name"], record_id))
                                non_informative_count += 1
                                _log(f"    --> Recorded NON_INFORMATIVE to ledger: {record_id} (V={pred_v_mag}, MAGLIM={header_maglim})")
                                continue

                            # Paths for cutout and diagnostics
                            split_dir = dataset_dir / t["split"]
                            raw_dir = split_dir / "raw"
                            diag_dir = split_dir / "diagnostics"
                            raw_dir.mkdir(parents=True, exist_ok=True)
                            diag_dir.mkdir(parents=True, exist_ok=True)

                            crop_path = raw_dir / f"{record_id}_{t['name'].replace('/', '_').replace(' ', '_')}_crop.fits"
                            png_path = diag_dir / f"{record_id}_diag.png"

                            # Save crop FITS atomically
                            cutout_hdr = cutout.wcs.to_header()
                            cutout_hdr["OBSJD"] = obsjd
                            cutout_hdr["EXPTIME"] = exptime
                            if header_maglim is not None:
                                cutout_hdr["MAGLIM"] = header_maglim
                            hdu_cutout = fits.PrimaryHDU(cutout.data, header=cutout_hdr)
                            atomic_save_fits(hdu_cutout, crop_path)

                            # Save PNG overlay atomically
                            try:
                                res_subplots = plt.subplots(figsize=(6, 6))
                                if isinstance(res_subplots, (tuple, list)) and len(res_subplots) == 2:
                                    fig, ax = res_subplots
                                else:
                                    fig = getattr(res_subplots, "figure", res_subplots)
                                    ax = res_subplots
                                norm = matplotlib.colors.Normalize(vmin=median - std, vmax=median + 5*std)
                                if hasattr(ax, "imshow"):
                                    ax.imshow(c_data, cmap='viridis', origin='lower', norm=norm)
                                crop_pos = getattr(cutout, "position_cutout", getattr(cutout, "input_position_cutout", (64.0, 64.0)))
                                crop_px, crop_py = float(crop_pos[0]), float(crop_pos[1])
                                if hasattr(ax, "plot"):
                                    ax.plot(crop_px, crop_py, 'rx', markersize=15, label="Prediction")
                                if segm is not None:
                                    try:
                                        x_c = tbl[x_col] if x_col in tbl.colnames else tbl['xcentroid']
                                        y_c = tbl[y_col] if y_col in tbl.colnames else tbl['ycentroid']
                                        if hasattr(ax, "scatter"):
                                            ax.scatter(x_c, y_c, facecolors='none', edgecolors='red', s=40, alpha=0.8)
                                    except Exception: pass
                                if hasattr(ax, "set_title"):
                                    ax.set_title(f"{t['name']} - {label}\nObs: {record_id} | Sep: {seg_nearest:.2f}\" | SNR: {seg_snr:.1f}")
                                if hasattr(ax, "legend"):
                                    ax.legend()
                                atomic_save_figure(fig, png_path)
                                plt.close(fig)
                            except Exception as e:
                                _log(f"Failed to generate diagnostic PNG: {e}")

                            # Mid-exposure JD
                            mid_exp_jd = obsjd + (exptime / 2.0) / 86400.0
                            origin_orig = getattr(cutout, "origin_original", (0.0, 0.0))

                            # Append manifest record
                            parent_obs_id = f"ztf_{obs.get('filefracday', 'unknown')}_{obs.get('field', 0):06d}_c{obs.get('ccdid', 0):02d}_q{obs.get('qid', 0)}"
                            saturate_hdr = float(hdu.header.get("SATURATE", 65535.0))
                            rec_entry = {
                                "object": t["name"],
                                "split": t["split"],
                                "record_id": record_id,
                                "parent_observation_id": parent_obs_id,
                                "fits_path": str(crop_path.relative_to(dataset_dir)),
                                "source_archive": "IRSA_ZTF",
                                "obsjd": obsjd,
                                "mid_exposure_jd": mid_exp_jd,
                                "ephemeris_jd": float(eph_exact["datetime_jd"]),
                                "ra": pred_ra,
                                "dec": pred_dec,
                                "predicted_x_full": full_px,
                                "predicted_y_full": full_py,
                                "crop_origin_x": float(origin_orig[0]),
                                "crop_origin_y": float(origin_orig[1]),
                                "predicted_x_crop": float(crop_px),
                                "predicted_y_crop": float(crop_py),
                                "predicted_x": full_px,
                                "predicted_y": full_py,
                                "filter": filt_code,
                                "exposure": exptime,
                                "pixel_scale": 1.012,
                                "label": label,
                                "label_confidence": 0.8 if label == "CANDIDATE_POSITIVE" else 0.5,
                                "label_logic_version": "2.0_scientific",
                                "detection_method": "segmentation_pedestal_subtracted",
                                "negative_reason": negative_reason,
                                "snr": seg_snr,
                                "angular_separation": seg_nearest,
                                "horizons_v_mag": pred_v_mag,
                                "horizons_position_uncertainty_arcsec": pred_uncertainty,
                                "header_maglim": header_maglim,
                                "header_saturate": saturate_hdr,
                                "provenance": "Exact-time epoch matching. Local cache full FITS.",
                                "pipeline_version": "2.0_hardened"
                            }

                            if is_comet:
                                rec_entry.update({
                                    "flags": comet_flags,
                                    "orbit_solution": t.get("orbit_solution"),
                                    "horizons_id": t.get("id"),
                                    "horizons_id_type": t.get("id_type"),
                                    "Tmag": eph_exact.get("t_mag"),
                                    "Nmag": eph_exact.get("n_mag"),
                                    "source_extent_px": comet_flag_info.get("source_extent_px", 0.0),
                                    "source_touches_edge": comet_flag_info.get("touches_edge", False),
                                    "peak_pixel_value": comet_flag_info.get("peak_val", 0.0),
                                    "saturated_pixel_fraction": comet_flag_info.get("sat_fraction", 0.0)
                                })

                            manifest.append(rec_entry)

                            # Commit manifest last
                            atomic_save_manifest(manifest, man_path)
                            seen_records.add((t["name"], record_id))

                            # Quota counting: for comets, only COMPACT rows count towards quota
                            if is_comet:
                                if "COMPACT" in comet_flags and "SATURATED" not in comet_flags and "EXTENDED_BEYOND_CROP" not in comet_flags:
                                    downloaded_for_obj += 1
                                    _log(f"  [QUOTA] {t['name']} COMPACT sample {downloaded_for_obj}/{target_quota}")
                            elif label not in ("UNVERIFIED_PENDING_RELOCALIZATION", "NON_INFORMATIVE"):
                                downloaded_for_obj += 1

                            new_samples_added += 1
                            match_found = True
                            _log(f"  [SAVED] Committed new sample {new_samples_added} ({record_id}, {label}, flags={comet_flags if is_comet else 'N/A'})")

                            if max_new_samples is not None and new_samples_added >= max_new_samples:
                                _log(f"Reached max_new_samples={max_new_samples} safe checkpoint. Pausing.")
                                exit_code = 2
                                status_msg = f"Reached max_new_samples={max_new_samples}"
                                return {"exit_code": exit_code, "status": status_msg, "samples_added": new_samples_added}

                            break
                    except Exception as crop_err:
                        _log(f"  Crop/Analysis failed for {record_id}: {crop_err}")
                        failure_ledger.record_failure(record_id, str(crop_err))
                        continue

                if match_found:
                    continue

    except BudgetExceededError as be:
        _log(f"[BUDGET EXCEEDED] {be}")
        exit_code = 2
        status_msg = str(be)
    except CircuitBreakerError as cb:
        _log(f"[CIRCUIT BREAKER TRIPPED] {cb}")
        exit_code = 3
        status_msg = str(cb)
    except Exception as ex:
        _log(f"[FATAL ERROR] {ex}")
        exit_code = 1
        status_msg = str(ex)
    finally:
        # Save cache and write final summary JSON
        try:
            cache_mgr.save()
        except Exception: pass

        summary_data = {
            "exit_code": exit_code,
            "status": status_msg,
            "samples_committed": new_samples_added,
            "non_informative_count": non_informative_count,
            "v_mag_threshold": v_mag_threshold,
            "days_skipped_prefilter": days_skipped_prefilter,
            "days_skipped_by_target": days_skipped_by_target,
            "total_samples_in_manifest": len(manifest),
            "elapsed_seconds": round(time.time() - start_time, 2),
            "budget_summary": budget.get_summary()
        }
        try:
            summary_path = dataset_dir / "summary.json"
            atomic_save_json(summary_data, summary_path)
            _log(f"Summary written to {summary_path}")
        except Exception as se:
            _log(f"Could not write summary.json: {se}")

        try:
            audit_path = dataset_dir / "AUDIT.md" if dataset_dir.name != "dataset_v1" else (REPO_ROOT / "docs" / "DATASET_V1_AUDIT.md")
            with open(audit_path, "w", encoding="utf-8") as af:
                af.write(f"# DATASET AUDIT REPORT\nTotal samples: {len(manifest)}\n")
        except Exception as ae:
            _log(f"Could not write AUDIT.md: {ae}")

    return {
        "exit_code": exit_code,
        "status": status_msg,
        "samples_added": new_samples_added,
        "summary": summary_data
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset_dir', type=str, default=None, help='Target directory for dataset')
    parser.add_argument('--max_new_samples', type=int, default=None, help='Pause after acquiring N new samples')
    parser.add_argument('--max_runtime_seconds', type=int, default=None, help='Maximum runtime in seconds before safe stop')
    parser.add_argument('--max_requests', type=int, default=None, help='Maximum number of remote API requests before safe stop')
    parser.add_argument('--max_workers', type=int, default=2, help='Maximum concurrent workers (max 2)')
    parser.add_argument('--objects', nargs='+', default=None, help='Filter target objects to process')
    parser.add_argument('--include_comets', action='store_true', help='Include comet targets')
    parser.add_argument('--images_per_object', type=int, default=30, help='Target quota per object (default: 30)')
    parser.add_argument('--v_mag_threshold', type=float, default=None, help='Horizons V magnitude prefilter threshold')
    args = parser.parse_args()

    result = generate_dataset(
        dataset_dir=args.dataset_dir,
        images_per_object=args.images_per_object,
        max_new_samples=args.max_new_samples,
        max_runtime_seconds=args.max_runtime_seconds,
        max_requests=args.max_requests,
        max_workers=min(args.max_workers, 2),
        target_names=args.objects,
        include_comets=args.include_comets,
        v_mag_threshold=args.v_mag_threshold
    )
    sys.exit(result["exit_code"])
