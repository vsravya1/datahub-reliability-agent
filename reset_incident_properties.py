"""
Clears all incident_* custom properties from the source DataHub asset.

Run this once before recording your final demo video, so the DataHub Memory
tab shows a clean slate instead of accumulated practice-run data.

Run:
  python reset_incident_properties.py
"""

import json
import os
import requests

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)"


def get_current_properties(urn: str) -> dict:
    resp = requests.get(
        f"{GMS_URL}/entities/{requests.utils.quote(urn, safe='')}",
        headers={"X-RestLi-Protocol-Version": "2.0.0"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    aspects = data.get("aspects", {}) or data.get("value", {})
    return (
        aspects.get("com.linkedin.dataset.DatasetProperties", {})
        .get("value", {})
        .get("customProperties", {})
    )


def main():
    existing = get_current_properties(SOURCE_URN)
    incident_keys = [k for k in existing if k.startswith("incident_")]
    print(f"Found {len(incident_keys)} incident_* keys: {incident_keys}")

    if not incident_keys:
        print("Nothing to clean up.")
        return

    cleaned = {k: v for k, v in existing.items() if not k.startswith("incident_")}

    payload = {
        "proposal": {
            "entityType": "dataset",
            "entityUrn": SOURCE_URN,
            "changeType": "UPSERT",
            "aspectName": "datasetProperties",
            "aspect": {
                "value": json.dumps({"customProperties": cleaned}),
                "contentType": "application/json",
            },
        }
    }
    resp = requests.post(
        f"{GMS_URL}/aspects?action=ingestProposal",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=10,
    )
    resp.raise_for_status()
    print("Cleaned. All incident_* properties removed.")


if __name__ == "__main__":
    main()
