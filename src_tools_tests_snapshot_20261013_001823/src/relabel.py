import json
man_path = r'E:\Downloads\ASTRION\data\dataset_v1\manifest.json'
with open(man_path, 'r') as f: manifest = json.load(f)
for m in manifest:
    seg_nearest = m['angular_separation']
    seg_snr = m['snr']
    if seg_nearest <= 3.0 and seg_snr >= 10.0:
        m['label'] = 'CONFIRMED_POSITIVE'
        m['negative_reason'] = ''
    elif 3.0 < seg_nearest <= 15.0 and seg_snr >= 5.0:
        m['label'] = 'HARD_NEGATIVE'
        m['negative_reason'] = 'STATIONARY_SOURCE_OFFSET'
    elif seg_nearest > 15.0 or seg_snr < 5.0:
        m['label'] = 'HARD_NEGATIVE'
        m['negative_reason'] = 'NO_CANDIDATE_NEAR_PREDICTION'
    else:
        m['label'] = 'HUMAN_VERIFICATION_REQUIRED'
        m['negative_reason'] = 'AMBIGUOUS_SIGNAL'
with open(man_path, 'w') as f: json.dump(manifest, f, indent=2)
print('Relabeled existing 80 samples.')
