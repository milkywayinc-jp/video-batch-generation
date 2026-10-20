"""Read an entire native workbook's authored structure for a pinned release.

The workbook itself stays in Drive/private local state, outside the public repo.
"""
from __future__ import annotations

import re
from .common import digest, require
from .sheets import gws_get


def column_name(number):
    name = ""
    while number:
        number, index = divmod(number - 1, 26)
        name = chr(65 + index) + name
    return name


def capture_structure(spreadsheet_id):
    meta = gws_get(spreadsheet_id,
                   fields="spreadsheetId,properties,namedRanges,developerMetadata,sheets(properties,merges,conditionalFormats,tables,protectedRanges,basicFilter,filterViews,charts,slicers,bandedRanges,rowGroups,columnGroups,developerMetadata)")
    for sheet in meta["sheets"]:
        props = sheet["properties"]
        title = "'" + props["title"].replace("'", "''") + "'"
        grid = props["gridProperties"]
        chunks = []
        for first in range(1, grid["rowCount"] + 1, 300):
            last = min(first + 299, grid["rowCount"])
            result = gws_get(spreadsheet_id, ranges=[f"{title}!A{first}:{column_name(grid['columnCount'])}{last}"],
                             includeGridData=True,
                             fields="sheets(data(startRow,startColumn,rowMetadata,columnMetadata,rowData(values(userEnteredValue,userEnteredFormat,dataValidation,note,textFormatRuns,chipRuns))))")
            require(len(result.get("sheets", [])) == 1, "Template changed while reading")
            chunks.extend(result["sheets"][0].get("data", []))
        sheet["data"] = chunks
    return meta


def structure_hash(snapshot):
    # Excludes workbook title/ID so a named native copy can match its release.
    properties = {k: v for k, v in snapshot.get("properties", {}).items() if k != "title"}
    return digest({"properties": properties, "namedRanges": snapshot.get("namedRanges", []),
                   "developerMetadata": snapshot.get("developerMetadata", []), "sheets": snapshot["sheets"]})


def verify_template(spreadsheet_id, release):
    require(release.get("schema_version") == 1 and release.get("fingerprint_schema") == 2
            and release.get("version"), "Invalid template release/fingerprint schema")
    require(isinstance(release["version"], str) and re.fullmatch(r"[A-Za-z0-9_.-]+", release["version"]),
            "Invalid release version")
    require(re.fullmatch(r"[0-9a-f]{64}", release.get("structure_sha256", "")), "Invalid release fingerprint")
    first = capture_structure(spreadsheet_id)
    second = capture_structure(spreadsheet_id)
    require(structure_hash(first) == structure_hash(second), "Template changed during read; no copy sent")
    require(structure_hash(second) == release["structure_sha256"],
            "This is not the pinned template version. Use the preserved release, not the edited original")
    return second
