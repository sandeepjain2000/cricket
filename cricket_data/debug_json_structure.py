#!/usr/bin/env python3
"""
Debug script to inspect Cricsheet JSON structure
Shows exactly what the T20 and LOI JSON files contain
"""

import json
import tempfile
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

print("=" * 70)
print("  Inspecting Cricsheet JSON Structure")
print("=" * 70)
print()

# Download a small sample of T20 files
CRICSHEET_URL_T20 = "https://cricsheet.org/downloads/t20s_json.zip"
REQUEST_TIMEOUT = 120

print("Downloading T20 data sample...")
with tempfile.TemporaryDirectory() as temp_dir:
    zip_path = Path(temp_dir) / "t20.zip"
    with urlopen(CRICSHEET_URL_T20, timeout=REQUEST_TIMEOUT) as response:
        with open(zip_path, 'wb') as f:
            f.write(response.read())

    extract_dir = Path(temp_dir) / "t20_extract"
    with ZipFile(zip_path) as z:
        z.extractall(extract_dir)

    json_files = sorted(list(extract_dir.glob("**/*.json")))
    print(f"Found {len(json_files)} JSON files")
    print()

    # Inspect first file
    if json_files:
        first_file = json_files[0]
        print(f"Inspecting: {first_file.name}")
        print()

        with open(first_file, 'r', encoding='utf-8') as f:
            match_data = json.load(f)

        # Show top-level keys
        print("Top-level keys:")
        for key in match_data.keys():
            print(f"  - {key}")
        print()

        # Show info structure
        if 'info' in match_data:
            print("'info' keys:")
            for key in match_data['info'].keys():
                print(f"  - {key}")
            print()
            print(f"Teams: {match_data['info'].get('teams', [])}")
            print(f"Dates: {match_data['info'].get('dates', [])}")
            print()

        # Show innings structure
        if 'innings' in match_data:
            innings = match_data['innings']
            print(f"Number of innings: {len(innings)}")

            for inn_idx, inning in enumerate(innings[:1]):  # First inning only
                print()
                print(f"Inning {inn_idx + 1} keys:")
                for key in inning.keys():
                    print(f"  - {key}")

                if 'deliveries' in inning:
                    deliveries = inning['deliveries']
                    print(f"Number of deliveries: {len(deliveries)}")

                    if deliveries:
                        # Show structure of first delivery
                        first_delivery = deliveries[0]
                        print(f"\nFirst delivery structure:")
                        print(json.dumps(first_delivery, indent=2))

                        # Show a few more deliveries
                        for i, delivery in enumerate(deliveries[:3]):
                            print(f"\nDelivery {i}:")
                            for over_ball, delivery_data in delivery.items():
                                print(f"  Over.Ball: {over_ball}")
                                print(f"  Batter: {delivery_data.get('batter', 'N/A')}")
                                print(f"  Bowler: {delivery_data.get('bowler', 'N/A')}")
                                print(f"  Non-striker: {delivery_data.get('non_striker', 'N/A')}")
                                print(f"  Runs: {delivery_data.get('runs', {})}")
                                print(f"  Wickets: {delivery_data.get('wickets', [])}")
                                print()
