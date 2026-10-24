"""Data-catalog identity links from the activity stream.

LINK_RESOURCE_IDENTITY rows state which person a record belongs to, e.g.
"Data catalog linked search_428 (search_428) to g.silva76@gmail.com and user_13."
They are identity evidence (citable by activity_id) but say nothing about the
record's state, so they are recorded with presence UNKNOWN; state.py treats
them as proof of existence only when no other evidence describes the record.
"""

import re

from ..dataset import Dataset
from .model import UNKNOWN, Observation, default_category, emails_in_text, system_for, token

USER_IN_TEXT = re.compile(r"\buser_\d+\b")


def catalog_link_observations(dataset: Dataset) -> list[Observation]:
    out = []
    for a in dataset.activities:
        if a.event_type != "LINK_RESOURCE_IDENTITY" or a.reported_result != "SUCCESS":
            continue
        identifiers = emails_in_text(a.details) | {token("user", u) for u in USER_IN_TEXT.findall(a.details)}
        category = default_category(a.resource_id)
        # Rule 06 condition, stated by the attachment catalog itself.
        if "belongs only to" in a.details.lower():
            category = "SUPPORT_ATTACHMENT"
        out.append(
            Observation(
                file_id=a.activity_id,
                observed_at=a.event_time,
                system_id=system_for(a.resource_id, a.target_system),
                resource_id=a.resource_id,
                presence=UNKNOWN,
                category=category,
                identifiers=identifiers,
                source="activity:LINK_RESOURCE_IDENTITY",
                attributes={"details": a.details},
                captured_at=a.event_time,
            )
        )
    return out
