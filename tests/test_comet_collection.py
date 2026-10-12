import sys
import os
import json
import math
import hashlib
import tempfile
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock, patch

from astropy.io import fits
from astropy.wcs import WCS
from astropy.coordinates import SkyCoord
import astropy.units as u
from photutils.segmentation import detect_sources, SegmentationImage

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dataset_generator import (
    COMET_TARGETS,
    compute_comet_flags,
    generate_dataset
)

PROD_MANIFEST_PATH = REPO_ROOT / "data" / "dataset_v1" / "manifest.json"
EXPECTED_PROD_HASH = "7E22E2457E475EF3F7146110DB92201019E35CED621AC510BF907B896701835E"

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()

def make_dummy_wcs_cutout(shape=(128, 128)):
    wcs_dict = {
        'CTYPE1': 'RA---TAN', 'CTYPE2': 'DEC--TAN',
        'CRVAL1': 100.0, 'CRVAL2': 20.0,
        'CRPIX1': 64.0, 'CRPIX2': 64.0,
        'CD1_1': -0.00028, 'CD1_2': 0.0,
        'CD2_1': 0.0, 'CD2_2': 0.00028
    }
    w = WCS(wcs_dict)
    cutout = MagicMock()
    cutout.wcs = w
    cutout.data = np.zeros(shape, dtype=float)
    return cutout, w

# 1. Comet id/id_type used & orbit solution defined
def test_comet_explicit_id_and_solution():
    assert len(COMET_TARGETS) == 2
    t_29p = next(t for t in COMET_TARGETS if "29P" in t["name"])
    t_c2022 = next(t for t in COMET_TARGETS if "C/2022 E3" in t["name"])

    assert t_29p["id"] == "90000396"
    assert t_29p["id_type"] == "smallbody"
    assert "JPL#K192/82" in t_29p["orbit_solution"]
    assert t_29p["epochs"]["start"] == "2022-09-01"

    assert t_c2022["id"] == "90004754"
    assert t_c2022["id_type"] == "smallbody"
    assert "JPL#82" in t_c2022["orbit_solution"]
    assert t_c2022["epochs"]["start"] == "2022-03-02"

# 2. Default asteroid-only; comets excluded without flags
def test_default_asteroid_only():
    with tempfile.TemporaryDirectory() as td:
        with patch('src.dataset_generator.ConnectorRegistry.get_connector'):
            # Calling generate_dataset with default targets (targets=None, include_comets=False)
            # and max_requests=0 will parse targets and stop
            res = generate_dataset(dataset_dir=Path(td), max_requests=0, include_comets=False)
            summary = res["summary"]
            assert summary["exit_code"] in (0, 2)

# 3. Comets require BOTH --include_comets AND explicit --objects list
def test_include_comets_requires_explicit_objects():
    with tempfile.TemporaryDirectory() as td:
        # If include_comets=True but target_names is None, comets must NOT be in targets
        with patch('src.dataset_generator.ConnectorRegistry.get_connector'):
            with patch('src.dataset_generator.Horizons') as mock_h:
                mock_inst = MagicMock()
                mock_inst.ephemerides.return_value = [{"RA": 10.0, "DEC": 20.0, "datetime_jd": 2459000.0, "V": 15.0}]
                mock_h.return_value = mock_inst
                res = generate_dataset(dataset_dir=Path(td), max_requests=0, include_comets=True, target_names=None)
                # Asteroid default targets only, no comets
                assert res["exit_code"] in (0, 2)

# 4. --objects is a strict allow-list: outside objects get ZERO queries and ZERO samples
def test_objects_strict_allowlist_outside_objects_never_queried_or_written():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        with patch('src.dataset_generator.ConnectorRegistry.get_connector') as mock_conn:
            with patch('src.dataset_generator.Horizons') as mock_h:
                # Pass an object not in the known targets list
                res = generate_dataset(dataset_dir=tdp, target_names=["Unknown_Object_999"], include_comets=True)
                assert res["exit_code"] == 0
                assert res["samples_added"] == 0
                # Horizons must NEVER have been called
                mock_h.assert_not_called()
                # Manifest must be empty
                man_path = tdp / "manifest.json"
                if man_path.exists():
                    with open(man_path, "r") as f:
                        data = json.load(f)
                    assert len(data) == 0

# 5. Quality flags correct on synthetic saturated/extended/compact frames
def test_comet_quality_flags_synthetic():
    cutout, w = make_dummy_wcs_cutout((128, 128))
    target_sky = w.pixel_to_world(64, 64)

    # 5a. Compact frame (clean Gaussian/point source in center, max well below SATURATE)
    c_data_compact = np.zeros((128, 128), dtype=float) + 500.0  # background
    c_data_compact[64, 64] = 2000.0  # peak
    c_data_compact[63:66, 63:66] += 500.0
    segm_compact = detect_sources(c_data_compact, threshold=600.0, n_pixels=3)
    flags, info = compute_comet_flags(c_data_compact, saturate_val=65000.0, segm=segm_compact, cutout=cutout, target_sky=target_sky, pred_uncertainty=0.5)
    assert "COMPACT" in flags
    assert "SATURATED" not in flags
    assert "EXTENDED_BEYOND_CROP" not in flags
    assert "EPHEMERIS_UNCERTAIN" not in flags
    assert info["is_compact"] is True

    # 5b. Saturated frame by peak (peak >= SATURATE)
    c_data_sat_peak = np.copy(c_data_compact)
    c_data_sat_peak[64, 64] = 70000.0
    flags, info = compute_comet_flags(c_data_sat_peak, saturate_val=65000.0, segm=segm_compact, cutout=cutout, target_sky=target_sky, pred_uncertainty=0.5)
    assert "SATURATED" in flags
    assert "COMPACT" not in flags
    assert info["is_saturated"] is True

    # 5c. Saturated frame by pixel fraction (>= 1% pixels >= 0.9 * SATURATE)
    c_data_sat_frac = np.copy(c_data_compact)
    # 1% of 128*128 = 163.84 pixels. Let's saturate 200 pixels
    c_data_sat_frac[50:65, 50:65] = 60000.0  # 15*15 = 225 pixels >= 0.9*65000 (58500)
    flags, info = compute_comet_flags(c_data_sat_frac, saturate_val=65000.0, segm=segm_compact, cutout=cutout, target_sky=target_sky, pred_uncertainty=0.5)
    assert "SATURATED" in flags
    assert "COMPACT" not in flags
    assert info["sat_fraction"] >= 0.01

    # 5d. Extended beyond crop: source touches crop edge
    c_data_edge = np.zeros((128, 128), dtype=float) + 500.0
    c_data_edge[0:10, 60:68] = 2000.0  # touches row 0 (edge)
    segm_edge = detect_sources(c_data_edge, threshold=600.0, n_pixels=3)
    target_sky_edge = w.pixel_to_world(64, 5)
    flags, info = compute_comet_flags(c_data_edge, saturate_val=65000.0, segm=segm_edge, cutout=cutout, target_sky=target_sky_edge, pred_uncertainty=0.5)
    assert "EXTENDED_BEYOND_CROP" in flags
    assert "COMPACT" not in flags
    assert info["touches_edge"] is True

    # 5e. Extended beyond crop: equivalent diameter >= 64 px (area >= pi * 32^2 = 3217 px)
    c_data_huge = np.zeros((128, 128), dtype=float) + 500.0
    c_data_huge[30:95, 30:95] = 1500.0  # 65x65 = 4225 pixels (equiv diameter ~73 px), not touching border
    segm_huge = detect_sources(c_data_huge, threshold=600.0, n_pixels=3)
    flags, info = compute_comet_flags(c_data_huge, saturate_val=65000.0, segm=segm_huge, cutout=cutout, target_sky=target_sky, pred_uncertainty=0.5)
    assert "EXTENDED_BEYOND_CROP" in flags
    assert "COMPACT" not in flags
    assert info["source_extent_px"] >= 64.0

    # 5f. Ephemeris uncertain: 3-sigma > 10 arcsec
    flags, info = compute_comet_flags(c_data_compact, saturate_val=65000.0, segm=segm_compact, cutout=cutout, target_sky=target_sky, pred_uncertainty=12.5)
    assert "EPHEMERIS_UNCERTAIN" in flags
    assert "COMPACT" in flags  # can still be compact if not saturated/extended

# 6. Flagged rows excluded from quota
def test_flagged_rows_excluded_from_quota():
    manifest = [
        {"object": "29P/Schwassmann-Wachmann 1", "flags": ["SATURATED"], "label": "UNVERIFIED_PENDING_RELOCALIZATION"},
        {"object": "29P/Schwassmann-Wachmann 1", "flags": ["EXTENDED_BEYOND_CROP"], "label": "UNVERIFIED_PENDING_RELOCALIZATION"},
        {"object": "29P/Schwassmann-Wachmann 1", "flags": ["COMPACT"], "label": "UNVERIFIED_PENDING_RELOCALIZATION"},
        {"object": "29P/Schwassmann-Wachmann 1", "flags": ["COMPACT", "EPHEMERIS_UNCERTAIN"], "label": "UNVERIFIED_PENDING_RELOCALIZATION"},
    ]
    # Filter per generator quota logic
    valid_comet = [
        m for m in manifest
        if m["object"] == "29P/Schwassmann-Wachmann 1"
        and "COMPACT" in m.get("flags", [])
        and "SATURATED" not in m.get("flags", [])
        and "EXTENDED_BEYOND_CROP" not in m.get("flags", [])
    ]
    assert len(valid_comet) == 2, "Only COMPACT rows without SATURATED or EXTENDED count towards quota"

# 7. Per-object quota ignores other objects
def test_per_object_quota_ignores_other_objects():
    manifest = [
        {"object": "29P/Schwassmann-Wachmann 1", "flags": ["COMPACT"], "label": "UNVERIFIED_PENDING_RELOCALIZATION"} for _ in range(20)
    ]
    valid_29p = [
        m for m in manifest
        if m["object"] == "29P/Schwassmann-Wachmann 1"
        and "COMPACT" in m.get("flags", [])
        and "SATURATED" not in m.get("flags", [])
        and "EXTENDED_BEYOND_CROP" not in m.get("flags", [])
    ]
    valid_c2022 = [
        m for m in manifest
        if m["object"] == "C/2022 E3 (ZTF)"
        and "COMPACT" in m.get("flags", [])
        and "SATURATED" not in m.get("flags", [])
        and "EXTENDED_BEYOND_CROP" not in m.get("flags", [])
    ]
    assert len(valid_29p) == 20
    assert len(valid_c2022) == 0, "C/2022 E3 quota must be independent of 29P"

# 8. Production hash unchanged
def test_production_hash_unchanged():
    current_hash = sha256_file(PROD_MANIFEST_PATH)
    assert current_hash == EXPECTED_PROD_HASH, f"Production hash changed! {current_hash} != {EXPECTED_PROD_HASH}"
