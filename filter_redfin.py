import csv
import gzip
import io
import os
import sys
import urllib.request
from datetime import datetime

SOURCE_URL = (
    "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
    "redfin_market_tracker/county_market_tracker.tsv000.gz"
)

TARGET_COUNTIES = {
    "Davidson", "Rutherford", "Williamson", "Montgomery", "Sumner",
    "Wilson", "Maury", "Robertson", "Dickson", "Cheatham"
}

OUTPUT_PATH = "data/tennessee_county_housing.csv"


def pick(row, *names):
    for name in names:
        if name in row:
            return (row.get(name) or "").strip()
    return ""


def county_name(value):
    value = value.strip()
    if value.upper().endswith(", TN"):
        value = value[:-4]
    if value.lower().endswith(" county"):
        value = value[:-7]
    return value.strip().title()


def parse_month(value):
    value = value.strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], fmt).strftime("%Y-%m")
        except ValueError:
            pass
    return value[:7] if len(value) >= 7 else ""


def main():
    print("Downloading Redfin county dataset...")
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "Middle-Tennessee-Market-Share/1.0"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        compressed = response.read()

    print(f"Downloaded {len(compressed):,} bytes.")

    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as gz:
        text_stream = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        reader = csv.DictReader(text_stream, delimiter="\t")

        selected = {}
        for row in reader:
            state_code = pick(row, "STATE_CODE", "STATE CODE").upper()
            state_name = pick(row, "STATE").lower()
            if state_code != "TN" and state_name != "tennessee":
                continue

            region_type = pick(row, "REGION_TYPE", "REGION TYPE").lower()
            if region_type and "county" not in region_type:
                continue

            property_type = pick(row, "PROPERTY_TYPE", "PROPERTY TYPE").lower()
            if property_type not in ("", "all residential", "all residential homes", "all"):
                continue

            duration = pick(row, "PERIOD_DURATION", "PERIOD DURATION").lower()
            if duration and "month" not in duration and duration != "1":
                continue

            county = county_name(pick(row, "REGION", "REGION_NAME", "REGION NAME"))
            if county not in TARGET_COUNTIES:
                continue

            reporting_month = parse_month(pick(row, "PERIOD_BEGIN", "PERIOD BEGIN"))
            homes_sold_raw = pick(row, "HOMES_SOLD", "HOMES SOLD").replace(",", "")
            if not reporting_month or not homes_sold_raw or homes_sold_raw.upper() == "NA":
                continue

            try:
                homes_sold = int(round(float(homes_sold_raw)))
            except ValueError:
                continue

            selected[(reporting_month, county)] = {
                "Reporting Month": reporting_month,
                "County": county,
                "State": "Tennessee",
                "Residential Closings": homes_sold,
                "Source": "Redfin County Market Tracker",
            }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    rows = [selected[key] for key in sorted(selected)]

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "Reporting Month", "County", "State",
                "Residential Closings", "Source"
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows):,} filtered rows to {OUTPUT_PATH}.")
    if not rows:
        raise RuntimeError("No Tennessee county rows were found.")


if __name__ == "__main__":
    main()
