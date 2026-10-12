"""
Probe script comparing documented IRSA IBE SIZE/INTERSECT multi-day queries vs daily
on the known 2019-04-25 Bennu case, and checking whether metadata includes a limiting-magnitude column.

STRICT CONSTRAINTS:
- Maximum 5 requests total.
- Requires explicit user approval before execution.
- Read-only probe; does not write to data/ directory.
"""
import requests
from astropy.table import Table

def run_probe():
    print("=== IRSA IBE Discovery Probe (Bennu 2019-04-25) ===")
    url = "https://irsa.ipac.caltech.edu/ibe/search/ztf/products/sci"

    # Bennu approximate position on 2019-04-25 (JD ~ 2458598.69)
    # RA ~ 116.89 deg, Dec ~ 22.86 deg
    ra = 116.89
    dec = 22.86
    start_jd = 2458598.0
    end_jd = 2458600.0

    requests_count = 0

    # Query 1: Single-day query with POS and INTERSECT=COVERS
    print("\n--- Query 1: Single-day query (INTERSECT=COVERS) ---")
    p1 = {
        "POS": f"{ra:.6f},{dec:.6f}",
        "INTERSECT": "COVERS",
        "WHERE": f"obsjd>{start_jd} AND obsjd<{start_jd + 1.0}"
    }
    r1 = requests.get(url, params=p1, timeout=30)
    requests_count += 1
    print(f"Request 1 URL: {r1.url}")
    print(f"Status: {r1.status_code}, Length: {len(r1.text)} chars")

    if r1.status_code == 200 and len(r1.text.splitlines()) >= 4:
        t1 = Table.read(r1.text.splitlines(), format="ipac")
        print(f"Rows returned: {len(t1)}")
        print("Columns:", t1.colnames)
        mag_cols = [c for c in t1.colnames if "mag" in c.lower() or "lim" in c.lower()]
        print("Limiting magnitude candidates in columns:", mag_cols)
    else:
        print("No rows or error in Query 1")

    # Query 2: Multi-day query with POS and INTERSECT=COVERS
    print("\n--- Query 2: Multi-day query (INTERSECT=COVERS, 2 days) ---")
    p2 = {
        "POS": f"{ra:.6f},{dec:.6f}",
        "INTERSECT": "COVERS",
        "WHERE": f"obsjd>{start_jd} AND obsjd<{end_jd}"
    }
    r2 = requests.get(url, params=p2, timeout=30)
    requests_count += 1
    print(f"Request 2 URL: {r2.url}")
    print(f"Status: {r2.status_code}, Length: {len(r2.text)} chars")
    if r2.status_code == 200 and len(r2.text.splitlines()) >= 4:
        t2 = Table.read(r2.text.splitlines(), format="ipac")
        print(f"Rows returned: {len(t2)}")
    else:
        print("No rows or error in Query 2")

    # Query 3: Multi-day query with SIZE (box search, e.g. SIZE=0.2 deg)
    print("\n--- Query 3: Multi-day query with SIZE (SIZE=0.2, INTERSECT=COVERS) ---")
    p3 = {
        "POS": f"{ra:.6f},{dec:.6f}",
        "SIZE": "0.2",
        "INTERSECT": "COVERS",
        "WHERE": f"obsjd>{start_jd} AND obsjd<{end_jd}"
    }
    r3 = requests.get(url, params=p3, timeout=30)
    requests_count += 1
    print(f"Request 3 URL: {r3.url}")
    print(f"Status: {r3.status_code}, Length: {len(r3.text)} chars")
    if r3.status_code == 200 and len(r3.text.splitlines()) >= 4:
        t3 = Table.read(r3.text.splitlines(), format="ipac")
        print(f"Rows returned: {len(t3)}")
    else:
        print("No rows or error in Query 3")

    print(f"\nTotal probe requests executed: {requests_count} (<= 5).")

if __name__ == "__main__":
    run_probe()
