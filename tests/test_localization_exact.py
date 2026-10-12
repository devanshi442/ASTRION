import sys
import os
import json
import tempfile
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dataset_generator import (
    make_coarse_key,
    make_exact_horizons_key,
    make_exact_ztf_key,
    CacheV2Manager,
    FailureLedger,
    generate_dataset
)
from src.budget import RequestBudget, BudgetExceededError, CircuitBreakerError
from src.ingestion.ztf import ZTFConnector, ZTFQueryError, ZTFDownloadError

def test_cache_v2_keys_specificity():
    """Verify that Cache v2 produces specific keys incorporating schema version and parameters."""
    k_coarse = make_coarse_key("101955 Bennu", 110.123456, 20.654321, 2458649.0, 2458650.0)
    assert "101955 Bennu" in k_coarse
    assert "110.1235" in k_coarse
    assert "20.6543" in k_coarse
    assert "v2" in k_coarse

    k_horizons = make_exact_horizons_key("101955 Bennu", 2458649.123456)
    assert "horizons_exact_101955 Bennu_2458649.12346_v2" == k_horizons

    k_ztf = make_exact_ztf_key("101955 Bennu", 110.1234, 20.6543, 2458649.12345)
    assert "ztf_exact_101955 Bennu_110.1234_20.6543_2458649.12345_v2" == k_ztf

def test_cache_v2_manager_migration_and_isolation():
    """Verify CacheV2Manager only migrates non-empty entries and preserves corrupt files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        # Create a v1 cache with 1 non-empty and 1 empty entry
        v1_cache = {
            "key_empty": [],
            "key_valid": [{"record_id": "rec_1"}]
        }
        with open(td / "meta_cache.json", "w") as f:
            json.dump(v1_cache, f)

        # Initialize manager: should only migrate key_valid
        mgr = CacheV2Manager(td)
        assert mgr.get("key_empty_v2") is None
        assert mgr.get("key_valid_v2") == [{"record_id": "rec_1"}]

        # Verify old file is untouched
        with open(td / "meta_cache.json", "r") as f:
            v1_recheck = json.load(f)
        assert "key_empty" in v1_recheck

        # Write corrupt v2 cache and verify it preserves rather than resets
        with open(td / "meta_cache_v2.json", "w") as f:
            f.write("{invalid json corrupt")

        with pytest.raises(RuntimeError, match="Corrupt meta_cache_v2.json detected and quarantined"):
            CacheV2Manager(td)

def test_failure_ledger_cooldown():
    """Verify FailureLedger records attempts and enforces cool-down."""
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        ledger = FailureLedger(td / "failure_ledger.json")

        assert not ledger.is_in_cooldown("rec_test_1")
        ledger.record_failure("rec_test_1", "503 Server Error")

        assert ledger.is_in_cooldown("rec_test_1")
        assert ledger.data["rec_test_1"]["attempts"] == 1

def test_ztf_connector_query_metadata_error_handling():
    """Verify ZTFConnector raises ZTFQueryError on HTTP failure and does not return empty list."""
    connector = ZTFConnector()
    with patch('requests.get') as mock_get:
        # Mock 504 Gateway Timeout
        mock_resp = MagicMock()
        mock_resp.status_code = 504
        mock_resp.reason = "Gateway Time-out"
        mock_get.return_value = mock_resp

        with pytest.raises(ZTFQueryError):
            connector.query_metadata(ra=150.0, dec=20.0, max_retries=2)

def test_ztf_connector_query_metadata_non_retryable_400():
    """Verify ZTFConnector immediately raises ZTFQueryError on 400 Bad Request without retrying."""
    connector = ZTFConnector()
    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_get.return_value = mock_resp

        with pytest.raises(ZTFQueryError, match="Non-retryable HTTP 400"):
            connector.query_metadata(ra=150.0, dec=20.0, max_retries=3)
        assert mock_get.call_count == 1  # No retries!

def test_ztf_connector_valid_empty_response():
    """Verify ZTFConnector returns [] ONLY for valid HTTP 200 with empty table."""
    connector = ZTFConnector()
    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "\\char\n\\header\n| col1 |\n"  # 3 lines, less than 4
        mock_get.return_value = mock_resp

        res = connector.query_metadata(ra=150.0, dec=20.0)
        assert res == []
