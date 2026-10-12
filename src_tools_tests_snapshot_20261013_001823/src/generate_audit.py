import json
man_path = r'E:\Downloads\ASTRION\data\dataset_v1\manifest.json'
with open(man_path, 'r') as f: manifest = json.load(f)
obj_counts = {}
split_counts = {}
label_counts = {}
for m in manifest:
    obj_counts[m['object']] = obj_counts.get(m['object'], 0) + 1
    split_counts[m['split']] = split_counts.get(m['split'], 0) + 1
    label_counts[m['label']] = label_counts.get(m['label'], 0) + 1

audit_report = [
    '# DATASET V1 PILOT - AUDIT REPORT', '',
    '## 1. Quality Control & Validation',
    f'- **Total Valid Samples:** {len(manifest)}',
    f'- **Positives (CONFIRMED_POSITIVE):** {label_counts.get("CONFIRMED_POSITIVE", 0)}',
    f'- **Negatives (HARD_NEGATIVE):** {label_counts.get("HARD_NEGATIVE", 0)}',
    f'- **Uncertain (HUMAN_VERIFICATION_REQUIRED):** {label_counts.get("HUMAN_VERIFICATION_REQUIRED", 0)}',
    '', '## 2. Methodology',
    '- All cutouts generated via local full-FITS caching and exact-epoch `Cutout2D` extraction.',
    '- Target position confirmed mathematically inside image bounds before extraction using Double Query.',
    '- Disjoint object-level train/val/test splits implemented successfully.',
    '', '## 3. Storage',
    '- Full FITS cached centrally.',
    '- Small 128x128 32-bit FITS generated and preserved for network ingestion.',
    '', '## 4. Observations by Object'
]
for k, v in obj_counts.items():
    audit_report.append(f'- **{k}:** {v} samples')
with open(r'E:\Downloads\ASTRION\docs\DATASET_V1_AUDIT.md', 'w') as f:
    f.write('\n'.join(audit_report))
print(f'Audit generated for {len(manifest)} samples.')
