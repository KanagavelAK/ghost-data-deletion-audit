"""Evidence normalizer: every file under dataset/evidence -> one observation table."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from ..dataset import Dataset, EvidenceFile
from .catalog_links import catalog_link_observations
from .documents import parse_pdf
from .images import load_cache, parse_image
from .logs import parse_log
from .model import Coverage, Observation
from .records import parse_json, parse_text
from .sql_snapshots import parse_sql


@dataclass
class FileSummary:
    file_id: str
    kind: str
    observations: int
    coverage: int
    problems: list[str]


@dataclass
class EvidenceStore:
    observations: list[Observation]
    coverage: list[Coverage]
    texts: dict[str, str]  # file_id -> extracted document text (PDF), kept for later reasoning
    summaries: list[FileSummary]
    by_resource: dict[str, list[Observation]] = field(init=False)
    by_identifier: dict[str, list[Observation]] = field(init=False)

    def __post_init__(self):
        self.by_resource = defaultdict(list)
        self.by_identifier = defaultdict(list)
        for obs in self.observations:
            self.by_resource[obs.resource_id].append(obs)
            for tok in obs.all_identifiers:
                self.by_identifier[tok].append(obs)
        for items in self.by_resource.values():
            items.sort(key=lambda o: (o.observed_at, o.file_id, o.source))

    @property
    def problems(self) -> list[str]:
        return [p for s in self.summaries for p in s.problems]

    def history(self, resource_id: str, until: datetime | None = None) -> list[Observation]:
        """Observations of a resource in time order, optionally only those at or before `until`."""
        items = self.by_resource.get(resource_id, [])
        return [o for o in items if until is None or o.observed_at <= until]

    def coverage_for(self, system_id: str, until: datetime | None = None) -> list[Coverage]:
        """Listings of a system, including cross-system customer snapshots (system_id '*')."""
        return [c for c in self.coverage if c.system_id in (system_id, "*") and (until is None or c.observed_at <= until)]


def _kind(evidence: EvidenceFile) -> str:
    return evidence.path.suffix.lower().lstrip(".")


def load_evidence(dataset: Dataset) -> EvidenceStore:
    observations, coverage, texts, summaries = [], [], {}, []
    image_cache = load_cache()
    for evidence in sorted(dataset.evidence_files, key=lambda e: e.file_id):
        kind = _kind(evidence)
        if not evidence.path.exists():
            summaries.append(FileSummary(evidence.file_id, kind, 0, 0, [f"{evidence.file_id}: file missing ({evidence.file_path})"]))
            continue
        text = ""
        if kind == "sql":
            obs, cov, problems = parse_sql(evidence)
        elif kind == "json":
            obs, cov, problems = parse_json(evidence)
        elif kind == "txt":
            obs, cov, problems = parse_text(evidence)
        elif kind == "log":
            obs, cov, problems = parse_log(evidence)
        elif kind == "pdf":
            obs, cov, problems, text = parse_pdf(evidence)
        elif kind == "png":
            obs, cov, problems = parse_image(evidence, image_cache)
        else:
            obs, cov, problems = [], [], [f"{evidence.file_id}: unsupported file type {kind!r}"]
        for item in (*obs, *cov):
            item.captured_at = evidence.captured_at
        observations += obs
        coverage += cov
        if text:
            texts[evidence.file_id] = text
        summaries.append(FileSummary(evidence.file_id, kind, len(obs), len(cov), problems))
    links = catalog_link_observations(dataset)
    observations += links
    summaries.append(FileSummary("system_activity", "catalog-links", len(links), 0, []))
    return EvidenceStore(observations, coverage, texts, summaries)
