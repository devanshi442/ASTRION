import os
import sys
import json
import glob
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from astropy.io import fits
from astropy.wcs import WCS

def sha256_file(filepath):
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()

def run_audit(staging_dir_path=None):
    repo_root = Path(r"E:\Downloads\ASTRION")
    if staging_dir_path:
        staging_dir = Path(staging_dir_path)
    elif len(sys.argv) > 1 and sys.argv[1]:
        staging_dir = Path(sys.argv[1])
    else:
        work_copy = repo_root / "data" / "dataset_v2_staging_20261011_rec_work"
        staging_dir = work_copy if work_copy.exists() else (repo_root / "data" / "dataset_v2_staging_20261011_rec")
    prod_manifest = repo_root / "data" / "dataset_v1" / "manifest.json"
    cache_dir = repo_root / "data" / "cache" / "ztf_science"
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = reports_dir / f"audit_{timestamp}.md"

    lines = []
    def log(msg=""):
        lines.append(msg)
        print(msg)

    log(f"# Staging Dataset Audit Report")
    log(f"Generated at: {datetime.now(timezone.utc).isoformat()} (Local: {datetime.now().isoformat()})")
    log(f"Target Staging Directory: `{staging_dir}`\n")

    # 1. Production Manifest Hash
    log("## 1. Production Hash Verification")
    if prod_manifest.exists():
        prod_hash = sha256_file(prod_manifest)
        expected_hash = "7E22E2457E475EF3F7146110DB92201019E35CED621AC510BF907B896701835E"
        match = (prod_hash == expected_hash)
        log(f"- Path: `{prod_manifest}`")
        log(f"- SHA-256: `{prod_hash}`")
        log(f"- Expected: `{expected_hash}`")
        log(f"- Match: **{'PASS' if match else 'FAIL - PRODUCTION MODIFIED!'}**\n")
    else:
        log("- **FAIL: Production manifest does not exist!**\n")

    # 2. Manifest Validity and Counts
    man_path = staging_dir / "manifest.json"
    log("## 2. Manifest Validity and Counts")
    manifest_data = []
    manifest_valid = False
    if not man_path.exists():
        log("- **FAIL: Staging manifest.json does not exist!**\n")
    else:
        mtime = datetime.fromtimestamp(man_path.stat().st_mtime)
        log(f"- Manifest path: `{man_path}`")
        log(f"- Size: {man_path.stat().st_size} bytes")
        log(f"- Last Modified: {mtime.isoformat()}")
        try:
            with open(man_path, 'r', encoding='utf-8') as f:
                manifest_data = json.load(f)
            manifest_valid = True
            log(f"- JSON Parsing: **VALID** ({len(manifest_data)} entries)")
        except Exception as e:
            log(f"- JSON Parsing: **CORRUPT / ERROR**: {e}")

    if manifest_valid:
        from collections import Counter
        label_counts = Counter(m.get("label", "MISSING") for m in manifest_data)
        object_counts = Counter(m.get("object", "MISSING") for m in manifest_data)
        split_counts = Counter(m.get("split", "MISSING") for m in manifest_data)

        log("\n### Label Counts:")
        for k, v in label_counts.most_common():
            log(f"- `{k}`: {v}")

        log("\n### Object Counts:")
        for k, v in object_counts.most_common():
            log(f"- `{k}`: {v}")

        log("\n### Split Counts:")
        for k, v in split_counts.most_common():
            log(f"- `{k}`: {v}")

        log("\n### Cross-Tabulation (Object x Split x Label):")
        obj_split_label = Counter((m.get("object"), m.get("split"), m.get("label")) for m in manifest_data)
        for (obj, sp, lb), count in sorted(obj_split_label.items()):
            log(f"- {obj} | {sp} | {lb}: {count}")

    # 3. Duplicates in Manifest
    log("\n## 3. Duplicate Analysis in Manifest")
    if manifest_valid:
        obj_rec_pairs = Counter((m.get("object"), m.get("record_id")) for m in manifest_data)
        dup_obj_rec = {k: v for k, v in obj_rec_pairs.items() if v > 1}
        log(f"- Duplicate `(object, record_id)`: {len(dup_obj_rec)}")
        for k, v in dup_obj_rec.items():
            log(f"  - `{k}`: {v} occurrences")

        fits_paths = Counter(m.get("fits_path") for m in manifest_data)
        dup_fits = {k: v for k, v in fits_paths.items() if v > 1}
        log(f"- Duplicate `fits_path`: {len(dup_fits)}")
        for k, v in dup_fits.items():
            log(f"  - `{k}`: {v} occurrences")

    # 4. Missing Files vs Manifest
    log("\n## 4. Missing Files (Referenced in Manifest but Missing on Disk)")
    missing_fits = []
    if manifest_valid:
        for m in manifest_data:
            fp = m.get("fits_path")
            if not fp:
                missing_fits.append((m.get("record_id"), "EMPTY_PATH"))
            else:
                full_p = staging_dir / fp
                if not full_p.exists():
                    missing_fits.append((m.get("record_id"), fp))
        log(f"- Missing FITS files count: {len(missing_fits)}")
        for rec, fp in missing_fits:
            log(f"  - Record `{rec}` missing: `{fp}`")

    # 5. On-Disk Crops & Orphan Analysis
    log("\n## 5. Crop and PNG Analysis on Disk")
    actual_fits = list(staging_dir.rglob("*.fits"))
    actual_pngs = list(staging_dir.rglob("*.png"))
    log(f"- Total FITS files under staging: {len(actual_fits)}")
    log(f"- Total PNG files under staging: {len(actual_pngs)}")

    manifest_rel_fits = {Path(m["fits_path"]).as_posix() for m in manifest_data if "fits_path" in m}
    orphan_fits = []
    for f in actual_fits:
        rel = f.relative_to(staging_dir).as_posix()
        if rel not in manifest_rel_fits:
            orphan_fits.append(rel)
    log(f"- Orphan FITS files (not in manifest): {len(orphan_fits)}")
    for o in orphan_fits:
        log(f"  - `{o}`")

    manifest_rec_ids = {m["record_id"] for m in manifest_data if "record_id" in m}
    orphan_pngs = []
    for p in actual_pngs:
        # Check if record_id is in filename
        matched = any(rec in p.name for rec in manifest_rec_ids)
        if not matched:
            orphan_pngs.append(p.relative_to(staging_dir).as_posix())
    log(f"- Orphan PNG files (no matching record_id in manifest): {len(orphan_pngs)}")
    for o in orphan_pngs:
        log(f"  - `{o}`")

    # 6. Detailed Crop Verification (Opens, 128x128, Celestial WCS)
    log("\n## 6. Crop File Integrity & Header Verification")
    invalid_crops = []
    crop_shapes = set()
    wcs_issues = []
    for f in actual_fits:
        try:
            with fits.open(f) as hdul:
                data = hdul[0].data
                header = hdul[0].header
                if data is None:
                    invalid_crops.append((f.name, "NO_DATA"))
                    continue
                shape = tuple(data.shape)
                crop_shapes.add(shape)
                if shape != (128, 128):
                    invalid_crops.append((f.name, f"SHAPE_{shape}"))
                try:
                    wcs = WCS(header)
                    if not wcs.has_celestial:
                        wcs_issues.append((f.name, "NO_CELESTIAL_WCS"))
                except Exception as we:
                    wcs_issues.append((f.name, f"WCS_ERROR: {we}"))
        except Exception as fe:
            invalid_crops.append((f.name, f"OPEN_ERROR: {fe}"))

    log(f"- Total crops inspected: {len(actual_fits)}")
    log(f"- Distinct crop shapes observed: {crop_shapes}")
    log(f"- Non-(128, 128) or corrupt crops: {len(invalid_crops)}")
    for name, reason in invalid_crops:
        log(f"  - `{name}`: {reason}")
    log(f"- WCS issues: {len(wcs_issues)}")
    for name, reason in wcs_issues:
        log(f"  - `{name}`: {reason}")

    # 7. Zero-length / .tmp / .part files in staging and cache
    log("\n## 7. Zero-Length, .tmp, and .part Files")
    for check_dir, label in [(staging_dir, "Staging"), (cache_dir, "Cache (ztf_science)")]:
        log(f"### Inspecting {label}: `{check_dir}`")
        if not check_dir.exists():
            log(f"- Directory does not exist!")
            continue
        all_files = list(check_dir.rglob("*"))
        zero_len = [f for f in all_files if f.is_file() and f.stat().st_size == 0]
        tmp_files = [f for f in all_files if f.name.endswith(".tmp") or ".tmp" in f.name]
        part_files = [f for f in all_files if f.name.endswith(".part") or ".part" in f.name]
        log(f"- Zero-length files: {len(zero_len)}")
        for z in zero_len:
            log(f"  - `{z.relative_to(check_dir)}`")
        log(f"- `.tmp` files: {len(tmp_files)}")
        for t in tmp_files:
            log(f"  - `{t.relative_to(check_dir)}` ({t.stat().st_size} bytes)")
        log(f"- `.part` files: {len(part_files)}")
        for p in part_files:
            log(f"  - `{p.relative_to(check_dir)}` ({p.stat().st_size} bytes)")

    # 8. Cache-dir FITS validity for files newer than 2026-10-11 15:30
    log("\n## 8. Cache Directory FITS Validity (Files newer than 2026-10-11 15:30:00)")
    cutoff_dt = datetime(2026, 10, 11, 15, 30, 0)
    cutoff_ts = cutoff_dt.timestamp()
    cache_fits = list(cache_dir.glob("*.fits"))
    recent_fits = [f for f in cache_fits if f.stat().st_mtime >= cutoff_ts]
    log(f"- Total FITS in cache: {len(cache_fits)}")
    log(f"- Files newer than {cutoff_dt.isoformat()}: {len(recent_fits)}")
    recent_valid = 0
    recent_invalid = []
    for f in recent_fits:
        mtime = datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        size = f.stat().st_size
        try:
            with fits.open(f) as hdul:
                shape = hdul[0].data.shape if hdul[0].data is not None else None
                hdr_obsjd = hdul[0].header.get("OBSJD")
                recent_valid += 1
                log(f"  - `{f.name}`: size={size} bytes, mtime={mtime}, shape={shape}, OBSJD={hdr_obsjd} (VALID)")
        except Exception as e:
            recent_invalid.append((f.name, size, str(e)))
            log(f"  - `{f.name}`: size={size} bytes, mtime={mtime} (CORRUPT: {e})")
    log(f"- Recent Cache FITS Summary: {recent_valid} valid, {len(recent_invalid)} invalid.")

    # 9. Cache Stats (meta_cache.json)
    log("\n## 9. Metadata Cache Analysis (`meta_cache.json`)")
    meta_cache_path = staging_dir / "meta_cache.json"
    if not meta_cache_path.exists():
        log("- **FAIL: meta_cache.json does not exist!**")
    else:
        try:
            with open(meta_cache_path, 'r', encoding='utf-8') as f:
                meta_cache = json.load(f)
            total_keys = len(meta_cache)
            empty_keys = [k for k, v in meta_cache.items() if v == []]
            non_empty_keys = [k for k, v in meta_cache.items() if v != []]
            exact_keys = [k for k in meta_cache.keys() if "_exact_" in k]
            coarse_keys = [k for k in meta_cache.keys() if "_exact_" not in k]

            # Check schema of non-empty entries
            schema_keys_expected = {"record_id", "obsjd", "original_uri", "filefracday", "field", "ccdid", "qid"}
            keys_without_schema = []
            for k in non_empty_keys:
                items = meta_cache[k]
                if not isinstance(items, list):
                    keys_without_schema.append((k, "NOT_A_LIST"))
                else:
                    for idx, item in enumerate(items):
                        if not isinstance(item, dict) or not schema_keys_expected.issubset(set(item.keys())):
                            keys_without_schema.append((k, f"ITEM_{idx}_MISSING_SCHEMA"))
                            break

            log(f"- Total Cache Keys: {total_keys}")
            log(f"- Empty Entries `[]`: {len(empty_keys)} ({len(empty_keys)/total_keys*100:.1f}%)")
            log(f"- Non-Empty Entries: {len(non_empty_keys)}")
            log(f"- `_exact_` Keys: {len(exact_keys)} (Empty: {sum(1 for k in exact_keys if meta_cache[k] == [])}, Non-empty: {sum(1 for k in exact_keys if meta_cache[k] != [])})")
            log(f"- Coarse Keys: {len(coarse_keys)} (Empty: {sum(1 for k in coarse_keys if meta_cache[k] == [])}, Non-empty: {sum(1 for k in coarse_keys if meta_cache[k] != [])})")
            log(f"- Non-empty keys without expected schema: {len(keys_without_schema)}")
            if keys_without_schema:
                for k, reason in keys_without_schema:
                    log(f"  - `{k}`: {reason}")
        except Exception as e:
            log(f"- **ERROR parsing meta_cache.json: {e}**")

    # 10. Manifest Diff against Backups & 4:01 PM Write Analysis
    log("\n## 10. Manifest Comparison against Backups & 4:01 PM Write Explanation")
    backups = [
        "manifest.json.bak",
        "manifest.json.audit_bak",
        "manifest.json.test_bak",
        "manifest_20261010_004739.bak",
        "manifest_pre_balance.json"
    ]

    current_recs = {m["record_id"]: m for m in manifest_data} if manifest_valid else {}

    for b_name in backups:
        b_path = staging_dir / b_name
        log(f"\n### Comparing against `{b_name}`:")
        if not b_path.exists():
            log(f"- File does not exist.")
            continue
        b_stat = b_path.stat()
        b_mtime = datetime.fromtimestamp(b_stat.st_mtime).isoformat()
        b_size = b_stat.st_size
        b_hash = sha256_file(b_path)
        log(f"- Size: {b_size} bytes, Mtime: {b_mtime}, SHA-256: `{b_hash}`")
        try:
            with open(b_path, 'r', encoding='utf-8') as f:
                b_data = json.load(f)
            b_recs = {m["record_id"]: m for m in b_data}
            log(f"- Total entries: {len(b_data)}")

            added_keys = set(current_recs.keys()) - set(b_recs.keys())
            removed_keys = set(b_recs.keys()) - set(current_recs.keys())
            common_keys = set(current_recs.keys()) & set(b_recs.keys())

            log(f"- Records added in current manifest: {len(added_keys)}")
            for k in list(added_keys)[:10]:
                log(f"  - Added: `{k}` ({current_recs[k].get('object')}, {current_recs[k].get('label')})")
            if len(added_keys) > 10:
                log(f"  - ... and {len(added_keys) - 10} more")

            log(f"- Records removed in current manifest: {len(removed_keys)}")
            for k in list(removed_keys)[:10]:
                log(f"  - Removed: `{k}` ({b_recs[k].get('object')}, {b_recs[k].get('label')})")
            if len(removed_keys) > 10:
                log(f"  - ... and {len(removed_keys) - 10} more")

            # Content changes for common records
            changed_fields = 0
            for k in common_keys:
                if current_recs[k] != b_recs[k]:
                    changed_fields += 1
            log(f"- Existing records with modified fields: {changed_fields}")

        except Exception as be:
            log(f"- Error reading backup: {be}")

    # Inspect creation/modification timestamps of newest samples
    log("\n### Newest Sample Analysis (Explaining 4:01 PM Write):")
    if manifest_valid and manifest_data:
        # Find newest sample mentioned in prompt: ztf_20190425195093_000566_c13_q4
        target_sample_id = "ztf_20190425195093_000566_c13_q4"
        sample_entry = next((m for m in manifest_data if m.get("record_id") == target_sample_id), None)
        if sample_entry:
            sample_fits = staging_dir / sample_entry["fits_path"]
            log(f"- Target sample `{target_sample_id}` found in manifest:")
            log(f"  - Object: {sample_entry.get('object')}")
            log(f"  - Label: {sample_entry.get('label')}")
            log(f"  - FITS Path: `{sample_entry.get('fits_path')}`")
            if sample_fits.exists():
                s_stat = sample_fits.stat()
                log(f"  - FITS Mtime: {datetime.fromtimestamp(s_stat.st_mtime).isoformat()}")
                log(f"  - FITS Ctime: {datetime.fromtimestamp(s_stat.st_ctime).isoformat()}")

        # Check all files in staging modified between 15:00 and 16:30 on 2026-10-11
        log("\n### Files under Staging modified between 15:00 and 16:30 on 2026-10-11:")
        window_start = datetime(2026, 10, 11, 15, 0, 0).timestamp()
        window_end = datetime(2026, 10, 11, 16, 30, 0).timestamp()
        recent_staging_files = []
        for p in staging_dir.rglob("*"):
            if p.is_file():
                mt = p.stat().st_mtime
                if window_start <= mt <= window_end:
                    recent_staging_files.append((mt, p))
        recent_staging_files.sort(key=lambda x: x[0])
        for mt, p in recent_staging_files:
            log(f"- {datetime.fromtimestamp(mt).isoformat()}: `{p.relative_to(staging_dir)}` ({p.stat().st_size} bytes)")

    # Save to report file
    with open(report_file, 'w', encoding='utf-8') as rf:
        rf.write("\n".join(lines))
    print(f"\nAudit completed. Report saved to: {report_file}")
    return report_file

if __name__ == '__main__':
    run_audit()
