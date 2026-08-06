import csv
import gzip
import io
import os
import shutil
import sys
import tempfile
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

LEGACY_SOURCE_URL = (
    "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
    "redfin_market_tracker/county_market_tracker.tsv000.gz"
)

SOURCE_URL = os.environ.get("REDFIN_SOURCE_URL", "").strip() or LEGACY_SOURCE_URL
OUTPUT_PATH = Path("data/tennessee_county_housing.csv")

TARGET_COUNTIES = {
    "Davidson",
    "Rutherford",
    "Williamson",
    "Montgomery",
    "Sumner",
    "Wilson",
    "Maury",
    "Robertson",
    "Dickson",
    "Cheatham",
}

OUTPUT_FIELDS = [
    "Reporting Month",
    "County",
    "State",
    "Residential Closings",
    "Source",
]


def pick(row: dict[str, str], *names: str) -> str:
    """Return the first non-empty value whose normalized header matches."""
    normalized = {
        str(key).strip().upper().replace("_", " "): value
        for key, value in row.items()
        if key is not None
    }

    for name in names:
        key = name.strip().upper().replace("_", " ")
        value = normalized.get(key)
        if value not in (None, ""):
            return str(value).strip()

    return ""


def normalize_county(value: str) -> str:
    value = (value or "").strip()

    if "," in value:
        value = value.split(",", 1)[0].strip()

    if value.lower().endswith(" county"):
        value = value[:-7].strip()

    return value.title()


def parse_month(value: str) -> str:
    text = (value or "").strip()

    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%Y-%m",
        "%m/%Y",
    ):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m")
        except ValueError:
            continue

    if len(text) >= 7 and text[4] in {"-", "/"}:
        candidate = text[:7].replace("/", "-")
        try:
            datetime.strptime(candidate, "%Y-%m")
            return candidate
        except ValueError:
            pass

    return ""


def parse_number(value: str) -> int | None:
    text = (value or "").replace(",", "").strip()

    if not text or text.upper() in {"NA", "N/A", "NULL", "-", "—"}:
        return None

    try:
        return int(round(float(text)))
    except ValueError:
        return None


def is_all_residential(value: str) -> bool:
    text = (value or "").strip().lower()

    return text in {
        "",
        "all residential",
        "all residential homes",
        "all residential properties",
        "all",
        "all homes",
        "all property types",
    }


def download_source(url: str) -> tuple[bytes, str]:
    print(f"Downloading Redfin source:\n{url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Middle-Tennessee-Market-Share/3.0",
            "Accept": "text/csv,text/tab-separated-values,application/gzip,*/*",
        },
    )

    with urllib.request.urlopen(request, timeout=300) as response:
        payload = response.read()
        content_type = response.headers.get("Content-Type", "")
        final_url = response.geturl()

    if not payload:
        raise RuntimeError("Redfin returned an empty response.")

    print(f"Downloaded {len(payload):,} bytes.")
    print(f"Content-Type: {content_type or 'not supplied'}")
    print(f"Final URL: {final_url}")

    return payload, final_url


def decode_payload(payload: bytes, source_url: str) -> str:
    is_gzip = payload[:2] == b"\x1f\x8b" or source_url.lower().endswith(".gz")

    if is_gzip:
        try:
            payload = gzip.decompress(payload)
        except OSError as exc:
            raise RuntimeError(
                "The source appeared to be gzip data but could not be decompressed."
            ) from exc

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise RuntimeError("Unable to decode the downloaded Redfin file.")


def detect_delimiter(text: str) -> str:
    sample = text[:100_000]

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        return dialect.delimiter
    except csv.Error:
        return "\t" if sample.count("\t") > sample.count(",") else ","


def iter_source_rows(text: str) -> Iterable[dict[str, str]]:
    delimiter = detect_delimiter(text)
    print(f"Detected delimiter: {repr(delimiter)}")

    stream = io.StringIO(text, newline="")
    reader = csv.DictReader(stream, delimiter=delimiter)

    if not reader.fieldnames:
        raise RuntimeError("No header row was detected in the Redfin file.")

    print("Detected columns:")
    print(", ".join(reader.fieldnames))

    yield from reader


def read_existing_rows() -> list[dict[str, str]]:
    if not OUTPUT_PATH.exists():
        return []

    with OUTPUT_PATH.open("r", newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def newest_month(rows: Iterable[dict[str, str]]) -> str:
    months = {
        str(row.get("Reporting Month", "")).strip()
        for row in rows
        if str(row.get("Reporting Month", "")).strip()
    }
    return max(months) if months else ""


def validate_complete_months(
    selected: dict[tuple[str, str], dict[str, object]]
) -> tuple[str, dict[str, set[str]]]:
    counties_by_month: dict[str, set[str]] = defaultdict(set)

    for month, county in selected:
        counties_by_month[month].add(county)

    complete_months = sorted(
        month
        for month, counties in counties_by_month.items()
        if counties == TARGET_COUNTIES
    )

    if not complete_months:
        details = "; ".join(
            f"{month}: {len(counties)}/10 counties"
            for month, counties in sorted(counties_by_month.items())[-6:]
        )
        raise RuntimeError(
            "No complete month containing all 10 required counties was found. "
            f"Recent month coverage: {details or 'none'}"
        )

    return complete_months[-1], counties_by_month


def expected_latest_month(today: date | None = None) -> str:
    """
    Conservative freshness expectation.

    Redfin monthly releases usually arrive during roughly the 8th-13th.
    Before the 16th, allow the source to contain data through two months ago.
    On/after the 16th, require the immediately preceding month.
    """
    today = today or date.today()
    first_this_month = today.replace(day=1)
    previous_month_end = first_this_month.fromordinal(
        first_this_month.toordinal() - 1
    )
    previous_month = previous_month_end.strftime("%Y-%m")

    if today.day >= 16:
        return previous_month

    two_months_ago_end = previous_month_end.replace(day=1).fromordinal(
        previous_month_end.replace(day=1).toordinal() - 1
    )
    return two_months_ago_end.strftime("%Y-%m")


def write_atomic(rows: list[dict[str, object]]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        newline="",
        encoding="utf-8",
        dir=OUTPUT_PATH.parent,
        prefix="tennessee_county_housing.",
        suffix=".tmp",
        delete=False,
    ) as temp:
        temp_path = Path(temp.name)
        writer = csv.DictWriter(temp, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    try:
        if OUTPUT_PATH.exists():
            backup = OUTPUT_PATH.with_suffix(".csv.bak")
            shutil.copy2(OUTPUT_PATH, backup)

        temp_path.replace(OUTPUT_PATH)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> None:
    payload, final_url = download_source(SOURCE_URL)
    text = decode_payload(payload, final_url)

    selected: dict[tuple[str, str], dict[str, object]] = {}
    tennessee_rows = 0
    target_county_rows = 0
    aggregate_rows = 0

    for row in iter_source_rows(text):
        state_code = pick(row, "STATE_CODE", "STATE CODE", "STATE ABBREVIATION").upper()
        state_name = pick(row, "STATE", "STATE NAME").lower()
        region = pick(
            row,
            "REGION",
            "REGION_NAME",
            "REGION NAME",
            "COUNTY",
            "COUNTY NAME",
        )

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

        region_type = pick(row, "REGION_TYPE", "REGION TYPE", "GEOGRAPHY TYPE").lower()
        if region_type and "county" not in region_type:
            continue

        property_type = pick(
            row,
            "PROPERTY_TYPE",
            "PROPERTY TYPE",
            "HOME TYPE",
        )
        if not is_all_residential(property_type):
            continue

        aggregate_rows += 1

        reporting_month = parse_month(
            pick(
                row,
                "PERIOD_BEGIN",
                "PERIOD BEGIN",
                "REPORTING MONTH",
                "MONTH",
                "DATE",
            )
        )
        homes_sold = parse_number(
            pick(
                row,
                "HOMES_SOLD",
                "HOMES SOLD",
                "RESIDENTIAL CLOSINGS",
                "CLOSED SALES",
            )
        )

        if not reporting_month or homes_sold is None:
            continue

        selected[(reporting_month, county)] = {
            "Reporting Month": reporting_month,
            "County": county,
            "State": "Tennessee",
            "Residential Closings": homes_sold,
            "Source": "Redfin Housing Market Tracker",
        }

    print(f"Tennessee rows found: {tennessee_rows:,}")
    print(f"Target-county rows found: {target_county_rows:,}")
    print(f"Aggregate residential rows found: {aggregate_rows:,}")

    newest_complete, counties_by_month = validate_complete_months(selected)
    expected = expected_latest_month()
    existing_rows = read_existing_rows()
    existing_latest = newest_month(existing_rows)

    print(f"Newest complete source month: {newest_complete}")
    print(f"Existing output latest month: {existing_latest or 'none'}")
    print(f"Minimum expected source month: {expected}")

    latest_counties = counties_by_month[newest_complete]
    missing = sorted(TARGET_COUNTIES - latest_counties)

    if missing:
        raise RuntimeError(
            f"Newest source month {newest_complete} is incomplete. "
            f"Missing counties: {', '.join(missing)}"
        )

    if newest_complete < expected:
        raise RuntimeError(
            "Redfin source is stale. "
            f"Newest complete month is {newest_complete}, but at least {expected} "
            "is expected. The existing CSV was preserved. Update the repository "
            "variable REDFIN_SOURCE_URL with a current Download Hub CSV URL."
        )

    rows = [selected[key] for key in sorted(selected)]

    if not rows:
        raise RuntimeError("No output rows were produced.")

    write_atomic(rows)

    print(f"Wrote {len(rows):,} rows to {OUTPUT_PATH}.")
    print(
        f"Latest month {newest_complete} contains all "
        f"{len(latest_counties)} required counties."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
