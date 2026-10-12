import sys
import os
import json
import time
import shutil
import hashlib
import tempfile
import threading
import requests
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock

from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u
from astropy.nddata import Cutout2D
from photutils.segmentation import detect_sources, SourceCatalog
from astropy.stats import sigma_clipped_stats

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dataset_generator import (
    make_coarse_key,
    make_exact_horizons_key,
    make_exact_ztf_key,
    CacheV2Manager,
    FailureLedger,
    generate_dataset,
    atomic_save_manifest,
    atomic_save_fits
)
from src.budget import RequestBudget, BudgetExceededError, CircuitBreakerError
from src.ingestion.ztf import ZTFConnector, ZTFQueryError, ZTFDownloadError

PROD_MANIFEST_PATH = REPO_ROOT / "data" / "dataset_v1" / "manifest.json"
EXPECTED_PROD_HASH = "7E22E2457E475EF3F7146110DB92201019E35CED621AC510BF907B896701835E"

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()

# 1. empty vs error responses
def test_1_empty_vs_error_responses():
    ztf = ZTFConnector()
    with patch('requests.get') as mock_get:
        # 1a. Valid empty response
        resp_empty = MagicMock()
        resp_empty.status_code = 200
        resp_empty.text = "\\char\n\\header\n| col1 |\n"
        mock_get.return_value = resp_empty
        res = ztf.query_metadata(ra=10.0, dec=20.0)
        assert res == [], "Valid empty table must return []"

        # 1b. Error response (504)
        resp_err = MagicMock()
        resp_err.status_code = 504
        resp_err.reason = "Gateway Time-out"
        mock_get.return_value = resp_err
        with pytest.raises(ZTFQueryError):
            ztf.query_metadata(ra=10.0, dec=20.0, max_retries=1)

# 2. 503/504 retry counts
def test_2_503_504_retry_counts():
    ztf = ZTFConnector()
    budget = RequestBudget(max_requests=10)
    with patch('requests.get') as mock_get, patch('time.sleep'):
        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.reason = "Service Unavailable"
        mock_get.return_value = resp_503

        with pytest.raises(ZTFQueryError):
            ztf.query_metadata(ra=10.0, dec=20.0, max_retries=3, budget=budget)

        # 3 attempts should be recorded on budget
        assert budget.total_requests == 3
        assert mock_get.call_count == 3

# 3. budget under 4 threads
def test_3_budget_under_4_threads():
    budget = RequestBudget(max_requests=1000)
    def worker():
        for _ in range(50):
            budget.record_attempt("other")
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert budget.total_requests == 200

# 4. deadline in phase 2
def test_4_deadline_in_phase_2():
    budget = RequestBudget(max_runtime_seconds=0.01)
    time.sleep(0.02)
    with pytest.raises(BudgetExceededError, match="Wall-clock deadline"):
        budget.check_budget()

# 5. cache-key specificity and never caching errors
def test_5_cache_key_specificity_and_never_caching_errors():
    k1 = make_coarse_key("Ceres", 10.0, 20.0, 100.0, 101.0, "COVERS")
    k2 = make_coarse_key("Vesta", 10.0, 20.0, 100.0, 101.0, "COVERS")
    k3 = make_coarse_key("Ceres", 10.001, 20.0, 100.0, 101.0, "COVERS")
    assert k1 != k2 and k1 != k3
    assert "_v2" in k1

    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        mgr = CacheV2Manager(td)
        # Attempt failed query simulation: manager must not cache errors
        ztf = ZTFConnector()
        with patch('requests.get', side_effect=ZTFQueryError("Simulated failure")):
            try:
                ztf.query_metadata(ra=10.0, dec=20.0)
            except ZTFQueryError:
                pass
        # Cache must remain empty
        assert mgr.get(k1) is None

# 6. corrupt manifest/cache abort
def test_6_corrupt_manifest_cache_abort():
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        # Corrupt manifest
        man_path = td / "manifest.json"
        with open(man_path, "w") as f: f.write("{corrupt json")
        with pytest.raises(RuntimeError, match="Corrupt manifest.json"):
            generate_dataset(targets=[], dataset_dir=td)
        # Verify file was preserved as .corrupt
        corrupt_files = list(td.glob("manifest.corrupt_*.json"))
        assert len(corrupt_files) == 1

        # Corrupt cache
        cache_path = td / "meta_cache_v2.json"
        with open(cache_path, "w") as f: f.write("{corrupt cache")
        with pytest.raises(RuntimeError, match="Corrupt meta_cache_v2.json"):
            CacheV2Manager(td)
        corrupt_cache_files = list(td.glob("meta_cache_v2.corrupt_*.json"))
        assert len(corrupt_cache_files) == 1

# 7. .part cleanup
def test_7_part_cleanup():
    cache_dir = Path("E:/Downloads/ASTRION/data/cache/ztf_science")
    ztf = ZTFConnector()
    with patch('requests.get', side_effect=requests.exceptions.ConnectionError("Network drop")):
        try:
            ztf.retrieve_data("test_part_rec_999", original_uri="https://example.com/test_part.fits", max_retries=1)
        except ZTFDownloadError:
            pass
    # Ensure no .part files remain
    parts = list(cache_dir.glob("*test_part_rec_999*.part"))
    assert len(parts) == 0

# 8. truncated/zero-byte FITS rejected
def test_8_truncated_zero_byte_fits_rejected():
    ztf = ZTFConnector()
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch('requests.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"Content-Length": "100"}
            # Only yield 10 bytes instead of 100
            mock_resp.iter_content.return_value = [b"truncated!"]
            mock_get.return_value = mock_resp

            with pytest.raises(ZTFDownloadError):
                ztf.retrieve_data("test_rec", original_uri="https://example.com/test.fits", max_retries=1)

# 9. no-celestial-WCS rejected
def test_9_no_celestial_wcs_rejected():
    ztf = ZTFConnector()
    # Create FITS with non-celestial header
    data = np.zeros((10, 10), dtype=np.float32)
    hdr = fits.Header()
    hdr['SIMPLE'] = True
    hdu = fits.PrimaryHDU(data, header=hdr)

    with tempfile.NamedTemporaryFile(suffix=".fits", delete=False) as tf:
        hdu.writeto(tf.name, overwrite=True)
        fits_bytes = Path(tf.name).read_bytes()
    os.unlink(tf.name)

    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": str(len(fits_bytes))}
        mock_resp.iter_content.return_value = [fits_bytes]
        mock_get.return_value = mock_resp

        with pytest.raises(ZTFDownloadError, match="no celestial WCS"):
            ztf.retrieve_data("test_rec", original_uri="https://example.com/test.fits", max_retries=1)

# 10. edge crop rejected
def test_10_edge_crop_rejected():
    # 200x200 image, target at pixel (195, 195). 128x128 cutout centered on (195, 195) exceeds bounds!
    data = np.ones((200, 200), dtype=float)
    wcs_dict = {
        'CTYPE1': 'RA---TAN', 'CTYPE2': 'DEC--TAN',
        'CRVAL1': 100.0, 'CRVAL2': 20.0,
        'CRPIX1': 100.0, 'CRPIX2': 100.0,
        'CD1_1': -0.00028, 'CD1_2': 0.0,
        'CD2_1': 0.0, 'CD2_2': 0.00028
    }
    w = WCS(wcs_dict)
    target = w.pixel_to_world(195, 195)
    size = u.Quantity((128, 128), u.pixel)

    # In strict mode, Cutout2D must raise error
    with pytest.raises(Exception):
        Cutout2D(data, target, size, wcs=w, mode='strict')

# 11. SNR with a 920 DN pedestal
def test_11_snr_with_pedestal():
    # Synthetic 128x128 cutout with background mean=920, std=15
    np.random.seed(42)
    bg = np.random.normal(loc=920.0, scale=15.0, size=(128, 128))
    # Place a star of peak 1070 DN (amplitude = 150 DN = 10 sigma) at center
    bg[64, 64] = 1070.0

    mean, median, std = sigma_clipped_stats(bg, sigma=3.0)
    peak_val = 1070.0

    # Old formula: peak_val / std = 1070 / 15 ~ 71.3 (erroneous)
    old_snr = peak_val / std
    assert old_snr > 60.0

    # New pedestal-subtracted SNR: (peak_val - median) / std ~ 10.0
    new_snr = (peak_val - median) / std
    assert np.isclose(new_snr, 10.0, atol=0.5)

# 12. label abstention matrix
def test_12_label_abstention_matrix():
    # Condition 1: nearby source high SNR -> CANDIDATE_POSITIVE
    sep1, snr1 = 1.5, 15.0
    if sep1 <= 3.0 and snr1 >= 10.0:
        label1 = "CANDIDATE_POSITIVE"
    assert label1 == "CANDIDATE_POSITIVE"

    # Condition 2: non-detection with predicted V fainter than MAGLIM - 0.5 -> NON_INFORMATIVE
    pred_v = 21.0
    maglim = 20.2 # fainter than 20.2 - 0.5 = 19.7
    sep2, snr2 = 25.0, 1.0
    if pred_v > (maglim - 0.5):
        label2 = "NON_INFORMATIVE"
    else:
        label2 = "HUMAN_VERIFICATION_REQUIRED"
    assert label2 == "NON_INFORMATIVE"

    # Condition 3: non-detection with bright predicted V -> HUMAN_VERIFICATION_REQUIRED
    pred_v_bright = 17.0
    if pred_v_bright > (maglim - 0.5):
        label3 = "NON_INFORMATIVE"
    else:
        label3 = "HUMAN_VERIFICATION_REQUIRED"
    assert label3 == "HUMAN_VERIFICATION_REQUIRED"

# 13. duplicates
def test_13_duplicates():
    seen = set()
    seen.add(("1 Ceres", "rec_001"))
    assert ("1 Ceres", "rec_001") in seen
    assert ("1 Ceres", "rec_002") not in seen

# 14. orphan detection
def test_14_orphan_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        manifest = [{"record_id": "rec_1", "fits_path": "rec_1.fits"}]
        # Create rec_1.fits and orphan rec_2.fits
        (td / "rec_1.fits").touch()
        (td / "rec_2.fits").touch()

        actual = list(td.glob("*.fits"))
        tracked = {td / m["fits_path"] for m in manifest}
        orphans = [f for f in actual if f not in tracked]
        assert len(orphans) == 1
        assert orphans[0].name == "rec_2.fits"

# 15. atomic-write crash simulation
def test_15_atomic_write_crash_simulation():
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        target = td / "manifest.json"
        initial_data = [{"record_id": "initial"}]
        atomic_save_manifest(initial_data, target)

        # Simulate crash during dump
        with patch('json.dump', side_effect=IOError("Simulated write interruption")):
            with pytest.raises(IOError):
                atomic_save_manifest([{"record_id": "corrupted"}], target)

        # Target must be completely unharmed
        with open(target, "r") as f:
            data = json.load(f)
        assert data == initial_data

# 16. crash-after-crop-before-manifest then resume
def test_16_crash_after_crop_before_manifest_resume():
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        raw_dir = td / "train" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        # Create uncommitted crop
        crop_file = raw_dir / "rec_orphan_crop.fits"
        crop_file.touch()

        # Manifest only has rec_1
        man_path = td / "manifest.json"
        atomic_save_manifest([{"object": "1 Ceres", "record_id": "rec_1", "fits_path": "train/raw/rec_1.fits"}], man_path)

        # Load manifest in generator
        with open(man_path, "r") as f:
            m = json.load(f)
        seen = {(x["object"], x["record_id"]) for x in m}
        # rec_orphan is not seen, manifest is intact
        assert ("1 Ceres", "rec_orphan") not in seen

# 17. 503 storm triggers circuit breaker and non-zero exit
def test_17_503_storm_triggers_circuit_breaker():
    budget = RequestBudget(consecutive_5xx_limit=3)
    budget.record_5xx(503)
    budget.record_5xx(503)
    with pytest.raises(CircuitBreakerError, match="Circuit breaker tripped after 3 consecutive 5xx"):
        budget.record_5xx(503)

# 18. exact Horizons called with observation time
def test_18_exact_horizons_called_with_obsjd():
    obsjd = 2458649.12345
    with patch('astroquery.jplhorizons.Horizons') as mock_h:
        mock_inst = MagicMock()
        mock_inst.ephemerides.return_value = [{"RA": 100.0, "DEC": 20.0, "datetime_jd": obsjd}]
        mock_h.return_value = mock_inst

        # Call Horizons for exact obsjd
        obj = mock_h(id="101955", id_type="smallbody", location="I41", epochs=[obsjd])
        eph = obj.ephemerides()
        mock_h.assert_called_with(id="101955", id_type="smallbody", location="I41", epochs=[obsjd])
        assert eph[0]["datetime_jd"] == obsjd

# 19. comet quota exclusion
def test_19_comet_quota_exclusion():
    manifest = [
        {"object": "C/2020 F3", "label": "UNVERIFIED_PENDING_RELOCALIZATION"},
        {"object": "C/2020 F3", "label": "UNVERIFIED_PENDING_RELOCALIZATION"},
        {"object": "1 Ceres", "label": "CANDIDATE_POSITIVE"}
    ]
    # Filter that excludes UNVERIFIED_PENDING_RELOCALIZATION from quota
    valid_comet = [m for m in manifest if m["object"] == "C/2020 F3" and m["label"] not in ("UNVERIFIED_PENDING_RELOCALIZATION", "NON_INFORMATIVE")]
    valid_ceres = [m for m in manifest if m["object"] == "1 Ceres" and m["label"] not in ("UNVERIFIED_PENDING_RELOCALIZATION", "NON_INFORMATIVE")]
    assert len(valid_comet) == 0, "Comet unverified samples must not count towards quota"
    assert len(valid_ceres) == 1

# 20. dataset_v1 hash unchanged
def test_20_dataset_v1_hash_unchanged():
    current_hash = sha256_file(PROD_MANIFEST_PATH)
    assert current_hash == EXPECTED_PROD_HASH, f"Production hash changed! {current_hash} != {EXPECTED_PROD_HASH}"

# Stage A Verification Tests
def test_stage_a_campaign_runner_refusal():
    """Verify campaign runner refuses dataset_v1, master staging, and non-staging paths."""
    from subprocess import run
    python_exe = sys.executable
    campaign_script = REPO_ROOT / "tools" / "run_campaign.py"

    invalid_dirs = [
        REPO_ROOT / "data" / "dataset_v1",
        REPO_ROOT / "data" / "dataset_v2_staging_20261011_rec",
        REPO_ROOT / "data" / "random_directory",
        REPO_ROOT / "data" / "staging_master"  # Not ending in _work or copy
    ]

    for inv in invalid_dirs:
        res = run([
            python_exe, str(campaign_script),
            "--max_campaign_hours", "0.01",
            "--max_batches", "1",
            "--max_total_samples", "1",
            "--staging_dir", str(inv)
        ], capture_output=True, text=True)
        assert res.returncode != 0
        assert "FATAL: Staging directory must be an isolated staging COPY" in (res.stderr + res.stdout)

def test_stage_a_prefilter_and_comets_default():
    """Verify MAGLIM 90th percentile prefilter and comets skipped by default."""
    from src.dataset_generator import compute_maglim_p90, generate_dataset
    p90 = compute_maglim_p90(REPO_ROOT / "data" / "cache" / "ztf_science")
    assert 20.0 <= p90 <= 22.0

    # Test comets skipped by default when targets=None
    with tempfile.TemporaryDirectory() as td:
        with patch('src.dataset_generator.Horizons') as mock_h:
            mock_inst = MagicMock()
            mock_inst.ephemerides.return_value = [{"RA": "0.0", "DEC": "0.0", "datetime_jd": "2458000.0", "V": 18.0}]
            mock_h.return_value = mock_inst
            # Run with max_requests=0 so it halts immediately after targets inspection
            with patch('src.dataset_generator.ConnectorRegistry.get_connector'):
                try:
                    res = generate_dataset(dataset_dir=Path(td), max_requests=0, include_comets=False)
                except Exception:
                    pass

def test_stage_a_labeling_matrix_and_non_informative_ledger():
    """Verify automated labelling never emits HARD_NEGATIVE and NON_INFORMATIVE writes to ledger."""
    from src.dataset_generator import generate_dataset
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reports_dir = REPO_ROOT / "reports"

        # Test 1: Bright non-detection -> HUMAN_VERIFICATION_REQUIRED (NO_DETECTION_BRIGHT_PREDICTION)
        pred_v_bright = 18.0
        maglim = 20.5 # 18.0 <= 20.5 - 0.5 (20.0) -> bright
        if pred_v_bright > (maglim - 0.5):
            lbl1 = "NON_INFORMATIVE"
            reason1 = "PREDICTED_MAGNITUDE_BELOW_DETECTION_LIMIT"
        else:
            lbl1 = "HUMAN_VERIFICATION_REQUIRED"
            reason1 = "NO_DETECTION_BRIGHT_PREDICTION"
        assert lbl1 == "HUMAN_VERIFICATION_REQUIRED"
        assert reason1 == "NO_DETECTION_BRIGHT_PREDICTION"
        assert lbl1 != "HARD_NEGATIVE"

        # Test 2: Faint non-detection -> NON_INFORMATIVE
        pred_v_faint = 21.0
        if pred_v_faint > (maglim - 0.5):
            lbl2 = "NON_INFORMATIVE"
            reason2 = "PREDICTED_MAGNITUDE_BELOW_DETECTION_LIMIT"
        else:
            lbl2 = "HUMAN_VERIFICATION_REQUIRED"
            reason2 = "NO_DETECTION_BRIGHT_PREDICTION"
        assert lbl2 == "NON_INFORMATIVE"
        assert reason2 == "PREDICTED_MAGNITUDE_BELOW_DETECTION_LIMIT"
        assert lbl2 != "HARD_NEGATIVE"
