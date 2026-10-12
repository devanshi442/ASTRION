"""
ASTRION Campaign Runner
Executes bounded batches on the staging copy ONLY (dataset_v2_staging_20261011_rec_work).
Strict safety guardrails, single-instance lock, sleep prevention, and automated audit checks.
"""
import sys
import os
import time
import json
import ctypes
import hashlib
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_MANIFEST = REPO_ROOT / "data" / "dataset_v1" / "manifest.json"
EXPECTED_PROD_HASH = "7E22E2457E475EF3F7146110DB92201019E35CED621AC510BF907B896701835E"
PYTHON_EXE = REPO_ROOT / "envs" / "probe" / "Scripts" / "python.exe"

# Windows Power Status Structure
class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("Reserved1", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong)
    ]

# Windows Execution State Flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

def set_sleep_prevention(enable: bool):
    try:
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception as e:
        print(f"Warning: could not set execution state: {e}", flush=True)

def check_ac_power() -> bool:
    try:
        status = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return status.ACLineStatus == 1
    except Exception:
        pass
    return True

def get_free_disk_gb(path: Path) -> float:
    try:
        import shutil
        total, used, free = shutil.disk_usage(str(path))
        return free / (1024 ** 3)
    except Exception:
        return 999.0

def sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()

def is_another_instance_running(lock_file: Path) -> bool:
    if lock_file.exists():
        try:
            with open(lock_file, "r") as f:
                content = f.read().strip()
            pid = int(content.split(":")[0]) if ":" in content else int(content)
            # Check if pid is running
            import psutil
            if psutil.pid_exists(pid):
                return True
        except Exception:
            # Fallback Windows check
            return True
    return False

def main():
    parser = argparse.ArgumentParser(description="ASTRION Bounded Batch Campaign Runner")
    parser.add_argument("--max_campaign_hours", type=float, required=True, help="Maximum campaign duration in hours (e.g. 3.0)")
    parser.add_argument("--max_batches", type=int, required=True, help="Maximum number of batches to run (e.g. 10)")
    parser.add_argument("--max_total_samples", type=int, default=40, help="Total sample cap across entire campaign (default: 40)")
    parser.add_argument("--batch_max_samples", type=int, default=5, help="Sample cap per batch (max 5)")
    parser.add_argument("--batch_max_seconds", type=int, default=600, help="Runtime cap per batch in seconds (max 600)")
    parser.add_argument("--batch_max_requests", type=int, default=100, help="Request cap per batch (max 100)")
    parser.add_argument("--batch_workers", type=int, default=2, help="Worker count (max 2)")
    parser.add_argument("--cooldown_seconds", type=int, default=300, help="Cool-down between batches in seconds (default 300 = 5 min)")
    parser.add_argument("--staging_dir", type=str, default=str(REPO_ROOT / "data" / "dataset_v2_staging_20261011_rec_work"), help="Staging work directory")
    parser.add_argument("--objects", nargs="+", default=None, help="Target objects to process (default: all configured targets)")
    parser.add_argument("--images_per_object", type=int, default=35, help="Per-object target quota (default: 35)")
    parser.add_argument("--include_comets", action="store_true", help="Include comet targets")
    args = parser.parse_args()

    # Safety clamps
    batch_samples = min(args.batch_max_samples, 5)
    batch_seconds = min(args.batch_max_seconds, 600)
    batch_requests = min(args.batch_max_requests, 100)
    batch_workers = min(args.batch_workers, 2)
    staging_dir = Path(args.staging_dir).resolve()

    # Strict isolation: refusal to touch production or outside staging work
    staging_str = str(staging_dir).lower()
    is_master_staging = (staging_dir == (REPO_ROOT / "data" / "dataset_v2_staging_20261011_rec").resolve())
    if "dataset_v1" in staging_str or is_master_staging or "staging" not in staging_str or not (staging_str.endswith("_work") or "copy" in staging_str or "work" in staging_str or "comet" in staging_str):
        sys.exit(f"FATAL: Staging directory must be an isolated staging COPY (refusing {staging_dir})!")

    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = reports_dir / f"campaign_{ts}.log"
    summary_file = reports_dir / "campaign_summary.json"
    lock_file = reports_dir / "campaign.lock"

    # Single-instance lock
    if is_another_instance_running(lock_file):
        sys.exit("FATAL: Another campaign or generator process is already running (lock file active). Refusing to start.")

    with open(lock_file, "w") as f:
        f.write(f"{os.getpid()}:{datetime.now().isoformat()}")

    def log(msg: str):
        line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}"
        print(line, flush=True)
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(line + "\n")

    log("=== ASTRION CAMPAIGN RUNNER STARTING ===")
    log(f"Staging Work Dir: {staging_dir}")
    log(f"Limits: max_hours={args.max_campaign_hours}, max_batches={args.max_batches}, max_total_samples={args.max_total_samples}")
    log(f"Batch config: max_samples={batch_samples}, max_seconds={batch_seconds}, max_requests={batch_requests}, workers={batch_workers}")

    # Prevent sleep
    set_sleep_prevention(True)

    campaign_start = time.time()
    max_duration = args.max_campaign_hours * 3600.0
    total_samples_accumulated = 0
    consecutive_zero_batches = 0
    batches_run = 0
    halt_reason = "Normal completion"

    summary_data = {
        "campaign_start": datetime.now(timezone.utc).isoformat(),
        "staging_dir": str(staging_dir),
        "batches_completed": 0,
        "total_new_samples": 0,
        "samples_by_label": {},
        "requests_by_type": {},
        "errors_by_type": {},
        "halt_reason": "",
        "elapsed_seconds": 0
    }

    try:
        while True:
            # 1. Campaign Limits Check
            elapsed = time.time() - campaign_start
            if elapsed >= max_duration:
                halt_reason = f"Campaign wall-clock cap of {args.max_campaign_hours} hours reached."
                log(halt_reason)
                break
            if batches_run >= args.max_batches:
                halt_reason = f"Campaign batch cap of {args.max_batches} batches reached."
                log(halt_reason)
                break
            if total_samples_accumulated >= args.max_total_samples:
                halt_reason = f"Campaign total sample cap of {args.max_total_samples} samples reached."
                log(halt_reason)
                break
            if consecutive_zero_batches >= 3:
                halt_reason = "HALT: 3 consecutive batches yielded zero new valid samples."
                log(halt_reason)
                break

            # 2. Safety Pre-Checks
            if not check_ac_power():
                halt_reason = "HALT: Laptop disconnected from AC power (running on battery)."
                log(halt_reason)
                break

            free_gb = get_free_disk_gb(staging_dir)
            if free_gb < 10.0:
                halt_reason = f"HALT: Free disk space below 10 GB ({free_gb:.2f} GB available)."
                log(halt_reason)
                break

            prod_hash = sha256_file(PROD_MANIFEST)
            if prod_hash != EXPECTED_PROD_HASH:
                halt_reason = f"HALT: Production manifest hash mismatch ({prod_hash} != {EXPECTED_PROD_HASH})!"
                log(halt_reason)
                break

            # Check manifest and cache parse
            man_path = staging_dir / "manifest.json"
            if man_path.exists():
                try:
                    with open(man_path, "r", encoding="utf-8") as f:
                        cur_man = json.load(f)
                except Exception as pe:
                    halt_reason = f"HALT: Staging manifest.json parse failure: {pe}"
                    log(halt_reason)
                    break

            # Run audit script before batch
            log(f"\n--- Running pre-batch audit (Batch {batches_run + 1}) ---")
            audit_script = REPO_ROOT / "tools" / "audit_staging.py"
            pre_audit = subprocess.run([str(PYTHON_EXE), str(audit_script), str(staging_dir)], capture_output=True, text=True)
            if pre_audit.returncode != 0:
                halt_reason = f"HALT: Pre-batch audit failed with exit code {pre_audit.returncode}"
                log(halt_reason)
                break

            # 3. Launch Batch
            batches_run += 1
            remaining_samples = max(0, args.max_total_samples - total_samples_accumulated)
            current_batch_samples = min(batch_samples, remaining_samples)
            log(f"\n=== Launching Batch {batches_run}/{args.max_batches} (batch cap: {current_batch_samples}, campaign remaining: {remaining_samples}) ===")

            len_before = len(cur_man)
            batch_summary_file = staging_dir / "summary.json"
            if batch_summary_file.exists():
                try: batch_summary_file.unlink()
                except OSError: pass

            cmd = [
                str(PYTHON_EXE),
                str(REPO_ROOT / "src" / "dataset_generator.py"),
                "--dataset_dir", str(staging_dir),
                "--max_new_samples", str(current_batch_samples),
                "--max_runtime_seconds", str(batch_seconds),
                "--max_requests", str(batch_requests),
                "--max_workers", str(batch_workers),
                "--images_per_object", str(args.images_per_object)
            ]
            if args.objects:
                cmd.extend(["--objects"] + args.objects)
            if args.include_comets:
                cmd.append("--include_comets")

            proc = subprocess.run(cmd, capture_output=True, text=True)
            log(f"Batch {batches_run} finished with exit code {proc.returncode}")
            if proc.stdout:
                for line in proc.stdout.strip().splitlines()[-10:]:
                    log(f"  [stdout] {line}")
            if proc.stderr:
                for line in proc.stderr.strip().splitlines()[-5:]:
                    log(f"  [stderr] {line}")

            # 4. Post-Batch Safety Checks
            # Re-check production hash
            prod_hash_after = sha256_file(PROD_MANIFEST)
            if prod_hash_after != EXPECTED_PROD_HASH:
                halt_reason = f"HALT: Production manifest modified during batch ({prod_hash_after})!"
                log(halt_reason)
                break

            # Read ground truth manifest delta
            new_samples_in_batch = 0
            if man_path.exists():
                try:
                    with open(man_path, "r", encoding="utf-8") as f:
                        post_man = json.load(f)
                    new_samples_in_batch = max(0, len(post_man) - len_before)
                except Exception as me:
                    log(f"Warning: could not read post-batch manifest: {me}")

            if batch_summary_file.exists():
                try:
                    with open(batch_summary_file, "r", encoding="utf-8") as f:
                        bsum = json.load(f)
                    for k, v in bsum.get("requests_by_type", {}).items():
                        summary_data["requests_by_type"][k] = summary_data["requests_by_type"].get(k, 0) + v
                    for k, v in bsum.get("errors_by_type", {}).items():
                        summary_data["errors_by_type"][k] = summary_data["errors_by_type"].get(k, 0) + v
                    for k, v in bsum.get("samples_by_label", {}).items():
                        summary_data["samples_by_label"][k] = summary_data["samples_by_label"].get(k, 0) + v
                except Exception:
                    pass

            log(f"Batch {batches_run} committed {new_samples_in_batch} new samples (manifest: {len_before} -> {len_before + new_samples_in_batch}).")

            total_samples_accumulated += new_samples_in_batch
            if new_samples_in_batch == 0:
                consecutive_zero_batches += 1
                log(f"Zero new samples in batch. Consecutive zero count: {consecutive_zero_batches}/3")
            else:
                consecutive_zero_batches = 0

            # Check if all requested target quotas are fulfilled
            if args.objects and man_path.exists():
                all_done = True
                for obj in args.objects:
                    if args.include_comets:
                        c_cnt = sum(
                            1 for m in post_man
                            if m["object"] == obj
                            and "COMPACT" in m.get("flags", [])
                            and "SATURATED" not in m.get("flags", [])
                            and "EXTENDED_BEYOND_CROP" not in m.get("flags", [])
                        )
                        target_q = min(args.images_per_object, 20)
                        if c_cnt < target_q:
                            all_done = False
                            break
                    else:
                        v_cnt = sum(
                            1 for m in post_man
                            if m["object"] == obj and m.get("label") not in ("UNVERIFIED_PENDING_RELOCALIZATION", "NON_INFORMATIVE")
                        )
                        if v_cnt < args.images_per_object:
                            all_done = False
                            break
                if all_done:
                    halt_reason = "All requested target quotas fulfilled."
                    log(f"\n[GOAL REACHED] {halt_reason}")
                    break

            # Check exit code
            # 0 = normal quota met, 2 = budget safe stop
            if proc.returncode == 3:
                halt_reason = "HALT: Circuit breaker tripped (consecutive 5xx errors)."
                log(halt_reason)
                break
            elif proc.returncode not in (0, 2):
                halt_reason = f"HALT: Unexpected batch non-zero exit code: {proc.returncode}"
                log(halt_reason)
                break

            # Run audit script after batch
            post_audit = subprocess.run([str(PYTHON_EXE), str(audit_script), str(staging_dir)], capture_output=True, text=True)
            if post_audit.returncode != 0:
                halt_reason = f"HALT: Post-batch audit failed with exit code {post_audit.returncode}"
                log(halt_reason)
                break

            # 5. Cooldown between batches
            log(f"Cool-down between batches: {args.cooldown_seconds}s...")
            time.sleep(args.cooldown_seconds)

    except KeyboardInterrupt:
        halt_reason = "Interrupted by user (KeyboardInterrupt)."
        log(halt_reason)
    except Exception as e:
        halt_reason = f"Fatal campaign exception: {e}"
        log(halt_reason)
    finally:
        # Restore sleep settings
        set_sleep_prevention(False)

        # Release lock file
        if lock_file.exists():
            try: lock_file.unlink()
            except OSError: pass

        # Compile final summary
        summary_data["batches_completed"] = batches_run
        summary_data["total_new_samples"] = total_samples_accumulated
        summary_data["halt_reason"] = halt_reason
        summary_data["elapsed_seconds"] = round(time.time() - campaign_start, 2)

        with open(summary_file, "w", encoding="utf-8") as sf:
            json.dump(summary_data, sf, indent=2)

        log("=== CAMPAIGN RUNNER FINISHED ===")
        log(f"Halt Reason: {halt_reason}")
        log(f"Batches Run: {batches_run}, Total Samples Added: {total_samples_accumulated}")
        log(f"Campaign summary saved to {summary_file}")

if __name__ == "__main__":
    main()
