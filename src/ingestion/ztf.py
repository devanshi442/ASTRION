import os
import sys
import time
import random
import hashlib
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from astropy.table import Table
from astropy.io import fits
from astropy.wcs import WCS
from .base import BaseIngestionConnector

class ZTFQueryError(Exception):
    """Raised when a ZTF metadata query fails."""
    pass

class ZTFDownloadError(Exception):
    """Raised when a ZTF FITS download or validation fails."""
    pass

def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)

class ZTFConnector(BaseIngestionConnector):
    @property
    def source_id(self) -> str:
        return "irsa_ztf"

    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "ephemeris": False,
            "image": True,
            "catalog": True,
        }

    def query_metadata(self, **kwargs) -> List[Dict[str, Any]]:
        """
        Query ZTF metadata from IRSA.
        Expects: ra (float), dec (float).
        Optional: start_jd, end_jd, timeout, max_retries, budget, req_type.
        Returns List of dicts on valid response, [] for empty result.
        Raises ZTFQueryError or BudgetExceededError on failure.
        """
        ra = kwargs.get("ra")
        dec = kwargs.get("dec")
        if ra is None or dec is None:
            raise ValueError("ra and dec are required.")

        url = "https://irsa.ipac.caltech.edu/ibe/search/ztf/products/sci"
        params = {"POS": f"{ra:.6f},{dec:.6f}", "INTERSECT": "COVERS"}

        start_jd = kwargs.get("start_jd")
        end_jd = kwargs.get("end_jd")
        where_clauses = []
        if start_jd is not None:
            where_clauses.append(f"obsjd>{start_jd}")
        if end_jd is not None:
            where_clauses.append(f"obsjd<{end_jd}")
        if where_clauses:
            params["WHERE"] = " AND ".join(where_clauses)

        timeout = kwargs.get("timeout", 30)
        max_retries = kwargs.get("max_retries", 3)
        budget = kwargs.get("budget", None)
        req_type = kwargs.get("req_type", "metadata_coarse")

        last_error = None
        for attempt in range(1, max_retries + 1):
            if budget is not None:
                budget.check_budget()
                budget.record_attempt(req_type)

            try:
                r = requests.get(url, params=params, timeout=timeout)
                raw_status = getattr(r, "status_code", 200)
                try:
                    status = int(raw_status)
                except (TypeError, ValueError):
                    status = 200

                # Check for retryable HTTP statuses
                if status in (429, 500, 502, 503, 504):
                    if budget is not None and status in (500, 502, 503, 504):
                        budget.record_5xx(status)
                    retry_after = r.headers.get("Retry-After")
                    sleep_time = float(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** (attempt - 1) + random.uniform(0.1, 0.5))
                    _log(f"ZTF metadata query received HTTP {status} (attempt {attempt}/{max_retries}). Backing off {sleep_time:.2f}s...")
                    last_error = ZTFQueryError(f"HTTP {status}: {r.reason} for {r.url}")
                    if attempt < max_retries:
                        time.sleep(sleep_time)
                        continue
                    else:
                        raise last_error

                # Non-retryable 4xx
                if 400 <= status < 500:
                    if budget is not None:
                        budget.record_error(f"HTTP_{status}")
                    raise ZTFQueryError(f"Non-retryable HTTP {status} from IRSA: {r.text[:200]}")

                r.raise_for_status()

                # If we get here, HTTP 200 succeeded
                if budget is not None:
                    budget.record_success()

                lines = r.text.splitlines()
                # If the response is empty or just header
                if len(lines) < 4:
                    return []

                tbl = Table.read(lines, format="ipac")
                results = []
                for row in tbl:
                    if "obsjd" not in row.colnames:
                        continue
                    ffd = str(int(row["filefracday"]))
                    field = int(row["field"])
                    ccdid = int(row["ccdid"])
                    qid = int(row["qid"])
                    filtercode = str(row["filtercode"])
                    imgtypecode = str(row["imgtypecode"])

                    original_uri = (f"https://irsa.ipac.caltech.edu/ibe/data/ztf/products/sci/"
                                    f"{ffd[:4]}/{ffd[4:8]}/{ffd[8:]}/"
                                    f"ztf_{ffd}_{field:06d}_{filtercode}_c{ccdid:02d}_"
                                    f"{imgtypecode}_q{qid}_sciimg.fits")

                    results.append({
                        "record_id": f"ztf_{ffd}_{field:06d}_c{ccdid:02d}_q{qid}",
                        "obsjd": float(row["obsjd"]),
                        "ra": float(row["ra"]) if "ra" in row.colnames else None,
                        "dec": float(row["dec"]) if "dec" in row.colnames else None,
                        "filtercode": filtercode,
                        "filefracday": ffd,
                        "field": field,
                        "ccdid": ccdid,
                        "imgtypecode": imgtypecode,
                        "qid": qid,
                        "original_uri": original_uri
                    })
                return results

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_error = e
                if budget is not None:
                    budget.record_error(e.__class__.__name__)
                sleep_time = 2 ** (attempt - 1) + random.uniform(0.1, 0.5)
                _log(f"ZTF metadata query network error (attempt {attempt}/{max_retries}): {e}. Backing off {sleep_time:.2f}s...")
                if attempt < max_retries:
                    time.sleep(sleep_time)
                else:
                    raise ZTFQueryError(f"ZTF metadata query network retries exhausted: {e}") from e
            except ZTFQueryError:
                raise
            except Exception as e:
                if budget is not None:
                    budget.record_error(e.__class__.__name__)
                raise ZTFQueryError(f"Unexpected error in ZTF metadata query: {e}") from e

        if last_error:
            raise ZTFQueryError(f"ZTF metadata query failed after {max_retries} retries: {last_error}")
        return []

    def retrieve_data(self, record_id: str, **kwargs) -> str:
        """
        Download and validate full ZTF FITS quadrant file.
        Accepts: original_uri, budget, expected_obsjd.
        Validates cache hits, writes atomically via unique .part file with fsync.
        Quarantines corrupt files and raises ZTFDownloadError on failure.
        """
        original_uri = kwargs.get("original_uri")
        if not original_uri:
            raise ValueError("original_uri is required for ZTF FITS retrieval.")

        budget = kwargs.get("budget", None)
        expected_obsjd = kwargs.get("expected_obsjd", None)
        cache_dir = Path("E:/Downloads/ASTRION/data/cache/ztf_science")
        cache_dir.mkdir(parents=True, exist_ok=True)

        uri_hash = hashlib.sha256(original_uri.encode('utf-8')).hexdigest()
        local_path = cache_dir / f"{record_id}_{uri_hash[:12]}.fits"

        # Check cache hit
        if local_path.exists():
            # Validate cache hit
            is_valid = False
            try:
                if local_path.stat().st_size > 0:
                    with fits.open(local_path) as hdul:
                        data = hdul[0].data
                        hdr = hdul[0].header
                        if data is not None and data.ndim == 2 and data.size > 0:
                            wcs = WCS(hdr)
                            if wcs.has_celestial:
                                is_valid = True
            except Exception as ex:
                _log(f"Cached FITS {local_path.name} failed validation: {ex}")
                is_valid = False

            if is_valid:
                _log(f"Cache hit (validated). Using local ZTF FITS {local_path.name}...")
                return str(local_path)
            else:
                ts = int(time.time())
                corrupt_path = cache_dir / f"{local_path.stem}.corrupt_{ts}.fits"
                _log(f"Quarantining corrupt cached FITS to {corrupt_path.name}")
                try:
                    local_path.rename(corrupt_path)
                except Exception:
                    pass

        # Cache miss or quarantined -> download fresh
        _log(f"Cache miss. Downloading full ZTF FITS from {original_uri}...")
        max_retries = kwargs.get("max_retries", 3)
        last_error = None

        for attempt in range(1, max_retries + 1):
            if budget is not None:
                budget.check_budget()
                budget.record_attempt("download")

            temp_path = local_path.with_name(f"{local_path.stem}_{os.getpid()}_{uuid.uuid4().hex[:8]}.part")
            try:
                r = requests.get(original_uri, stream=True, timeout=120)
                raw_status = getattr(r, "status_code", 200)
                try:
                    status = int(raw_status)
                except (TypeError, ValueError):
                    status = 200

                if status in (429, 500, 502, 503, 504):
                    if budget is not None and status in (500, 502, 503, 504):
                        budget.record_5xx(status)
                    retry_after = r.headers.get("Retry-After")
                    sleep_time = float(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** (attempt - 1) + random.uniform(0.1, 0.5))
                    _log(f"Download HTTP {status} (attempt {attempt}/{max_retries}). Backing off {sleep_time:.2f}s...")
                    last_error = ZTFDownloadError(f"HTTP {status} for download: {original_uri}")
                    if attempt < max_retries:
                        time.sleep(sleep_time)
                        continue
                    else:
                        raise last_error

                if 400 <= status < 500:
                    if budget is not None:
                        budget.record_error(f"HTTP_{status}")
                    raise ZTFDownloadError(f"Non-retryable download HTTP {status}: {original_uri}")

                r.raise_for_status()

                content_length = r.headers.get("Content-Length")
                expected_bytes = int(content_length) if content_length and content_length.isdigit() else None
                downloaded_bytes = 0

                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if budget is not None:
                            budget.check_budget()
                        if chunk:
                            f.write(chunk)
                            downloaded_bytes += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())

                if expected_bytes is not None and downloaded_bytes != expected_bytes:
                    raise ZTFDownloadError(f"Incomplete download: got {downloaded_bytes} bytes, expected {expected_bytes}")

                # Validate FITS integrity on the temporary file
                with fits.open(temp_path, memmap=False) as hdul:
                    data = hdul[0].data
                    hdr = hdul[0].header
                    if data is None or data.ndim != 2 or data.size == 0:
                        raise ZTFDownloadError("Downloaded file has no 2D image data HDU")
                    wcs = WCS(hdr)
                    if not wcs.has_celestial:
                        raise ZTFDownloadError("Downloaded FITS has no celestial WCS")
                    if expected_obsjd is not None:
                        hdr_obsjd = float(hdr.get("OBSJD", -999))
                        if abs(hdr_obsjd - expected_obsjd) > 1e-4:
                            raise ZTFDownloadError(f"Header OBSJD ({hdr_obsjd}) mismatch with metadata ({expected_obsjd}) > 1e-4 d")

                del data, hdr, hdul
                import gc
                gc.collect()

                # Atomically replace into final cache location with retry on Windows file lock
                replace_success = False
                for r_attempt in range(3):
                    try:
                        os.replace(temp_path, local_path)
                        replace_success = True
                        break
                    except OSError:
                        time.sleep(0.5)

                if not replace_success:
                    os.replace(temp_path, local_path)

                if budget is not None:
                    budget.record_success()

                _log(f"Successfully downloaded and verified {local_path.name} ({downloaded_bytes} bytes).")
                return str(local_path)

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_error = e
                if temp_path.exists():
                    try: os.unlink(temp_path)
                    except OSError: pass
                if budget is not None:
                    budget.record_error(e.__class__.__name__)
                sleep_time = 2 ** (attempt - 1) + random.uniform(0.1, 0.5)
                _log(f"Download network error (attempt {attempt}/{max_retries}): {e}. Backing off {sleep_time:.2f}s...")
                if attempt < max_retries:
                    time.sleep(sleep_time)
                else:
                    raise ZTFDownloadError(f"Download retries exhausted for {original_uri}: {e}") from e
            except Exception as e:
                if temp_path.exists():
                    try: os.unlink(temp_path)
                    except OSError: pass
                if isinstance(e, (ZTFDownloadError,)):
                    raise
                if budget is not None:
                    budget.record_error(e.__class__.__name__)
                raise ZTFDownloadError(f"Download failed for {original_uri}: {e}") from e

        if last_error:
            raise ZTFDownloadError(f"Download failed after {max_retries} attempts: {last_error}")
        raise ZTFDownloadError(f"Download failed for {record_id}")
