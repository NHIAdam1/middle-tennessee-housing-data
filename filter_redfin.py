import csv
import gzip
import io
import os
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


def normalize_county(value):
    value = (value or "").strip()

    # Examples handled:
    # Davidson County, TN
    # Davidson County
    # Davidson, TN
    if "," in value:
        value = value.split(",", 1)[0].strip()

    if value.lower().endswith(" county"):
        value = value[:-7].strip()

    return value.title()


def parse_month(value):
    text = (value or "").strip()

    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m")
        except ValueError:
            pass

    return text[:7] if len(text) >= 7 else ""


def parse_number(value):
    text = (value or "").replace(",", "").strip()

    if not text or text.upper() in {"NA", "N/A", "NULL"}:
        return None

    try:
        return int(round(float(text)))
    except ValueError:
        return None


def is_all_residential(value):
    text = (value or "").strip().lower()

    # Accept blank aggregate rows and common Redfin aggregate labels.
    return text in {
        "",
        "all residential",
        "all residential homes",
        "all",
        "all homes",
    }


def main():
    print("Downloading Redfin county dataset...")

    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "Middle-Tennessee-Market-Share/2.0"}
    )

    with urllib.request.urlopen(request, timeout=240) as response:
        compressed = response.read()

    print(f"Downloaded {len(compressed):,} bytes.")

    selected = {}
    tennessee_rows = 0
    target_county_rows = 0
    property_rows = 0

    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as gz:
        stream = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        reader = csv.DictReader(stream, delimiter="\t")

        print("Detected columns:")
        print(", ".join(reader.fieldnames or []))

        for row in reader:
            state_code = pick(row, "STATE_CODE", "STATE CODE").upper()
            state_name = pick(row, "STATE").lower()
            region = pick(row, "REGION", "REGION_NAME", "REGION NAME")

            # Some Redfin county rows expose Tennessee through the region
            # suffix even when state fields are blank.
            is_tennessee = (
                state_code == "TN"
                or state_name == "tennessee"
                or region.upper().endswith(", TN")
            )
            if not is_tennessee:
                continue

            tennessee_rows += 1

            county = normalize_county(region)
            if county not in TARGET_COUNTIES:
                continue

            target_county_rows += 1

            region_type = pick(row, "REGION_TYPE", "REGION TYPE").lower()
            if region_type and "county" not in region_type:
                continue

            property_type = pick(row, "PROPERTY_TYPE", "PROPERTY TYPE")
            if not is_all_residential(property_type):
                continue

            property_rows += 1

            reporting_month = parse_month(
                pick(row, "PERIOD_BEGIN", "PERIOD BEGIN")
            )
            homes_sold = parse_number(
                pick(row, "HOMES_SOLD", "HOMES SOLD")
            )

            if not reporting_month or homes_sold is None:
                continue

            selected[(reporting_month, county)] = {
                "Reporting Month": reporting_month,
                "County": county,
                "State": "Tennessee",
                "Residential Closings": homes_sold,
                "Source": "Redfin County Market Tracker",
            }

    print(f"Tennessee rows found: {tennessee_rows:,}")
    print(f"Target-county rows found: {target_county_rows:,}")
    print(f"Aggregate residential rows found: {property_rows:,}")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    rows = [selected[key] for key in sorted(selected)]

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "Reporting Month",
                "County",
                "State",
                "Residential Closings",
                "Source",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows):,} filtered rows to {OUTPUT_PATH}.")

    if not rows:
        raise RuntimeError(
            "No output rows were produced. Review the diagnostic counts "
            "and detected columns above."
        )


if __name__ == "__main__":
    main()
