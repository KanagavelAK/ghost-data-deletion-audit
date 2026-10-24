"""Local scorer that compares predicted output rows with canonical answers.

The hidden grader's exact formula is unknown, so this reports every dimension
the problem statement names: case status, step and field accuracy, sequence
order, identity resolution, resource discovery and evidence citations.
"""

from collections import Counter
from dataclasses import dataclass, field

STEP_KEY_FIELDS = ("phase", "operation", "system_id", "resource_id", "state", "evidence_id")
POSITION_FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def step_key(step: dict) -> tuple:
    return tuple(step.get(f, "") for f in STEP_KEY_FIELDS)


def struct_key(step: dict) -> tuple:
    """Step identity without the evidence citation."""
    return step_key(step)[:-1]


@dataclass
class PRF:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: "PRF") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def multiset_prf(pred: list, gold: list) -> PRF:
    p, g = Counter(pred), Counter(gold)
    tp = sum((p & g).values())
    return PRF(tp, sum(p.values()) - tp, sum(g.values()) - tp)


def lcs_length(a: list, b: list) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, start=1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def pairwise_order_agreement(pred: list, gold: list) -> float:
    """Share of pairs of common steps that appear in the same relative order."""
    gold_pos = {k: i for i, k in reversed(list(enumerate(gold)))}
    common = [gold_pos[k] for k in dict.fromkeys(pred) if k in gold_pos]
    pairs = [(a, b) for i, a in enumerate(common) for b in common[i + 1:]]
    if not pairs:
        return 1.0
    return sum(1 for a, b in pairs if a < b) / len(pairs)


@dataclass
class CaseScore:
    case_id: str
    gold_status: str
    pred_status: str
    exact: bool
    steps: PRF
    struct: PRF
    resources: PRF
    by_phase: dict[str, PRF]
    account_correct: bool | None
    evidence_hits: int
    evidence_total: int
    field_hits: Counter
    positions: int
    lcs_ratio: float
    order_agreement: float
    gold_operations: Counter = field(default_factory=Counter)
    missing: list[tuple] = field(default_factory=list)
    extra: list[tuple] = field(default_factory=list)

    @property
    def status_correct(self) -> bool:
        return self.gold_status == self.pred_status


def score_case(case_id: str, gold_status: str, gold_steps: list, pred_status: str, pred_steps: list) -> CaseScore:
    gk = [step_key(s) for s in gold_steps]
    pk = [step_key(s) for s in pred_steps]

    by_phase = {}
    for phase in ("FINDING", "REMEDIATION", "VERIFICATION"):
        by_phase[phase] = multiset_prf([k for k in pk if k[0] == phase], [k for k in gk if k[0] == phase])

    gold_accounts = {s["resource_id"] for s in gold_steps if s.get("system_id") == "account_service"}
    pred_accounts = {s.get("resource_id") for s in pred_steps if s.get("system_id") == "account_service"}

    # A cited gold step counts as a hit when the same step is predicted with the same evidence.
    pred_struct_evidence = Counter((struct_key(s), s.get("evidence_id", "")) for s in pred_steps)
    cited = [s for s in gold_steps if s.get("evidence_id")]
    evidence_hits = sum(1 for s in cited if pred_struct_evidence[(struct_key(s), s["evidence_id"])] > 0)

    field_hits = Counter()
    positions = max(len(gold_steps), len(pred_steps))
    for g, p in zip(gold_steps, pred_steps):
        for name in POSITION_FIELDS:
            if g.get(name) == p.get(name):
                field_hits[name] += 1

    gold_count, pred_count = Counter(gk), Counter(pk)
    return CaseScore(
        case_id=case_id,
        gold_status=gold_status,
        pred_status=pred_status,
        exact=gold_status == pred_status and gk == pk,
        steps=multiset_prf(pk, gk),
        struct=multiset_prf([k[:-1] for k in pk], [k[:-1] for k in gk]),
        resources=multiset_prf(
            list({(s.get("system_id"), s.get("resource_id")) for s in pred_steps}),
            list({(s["system_id"], s["resource_id"]) for s in gold_steps}),
        ),
        by_phase=by_phase,
        account_correct=(pred_accounts == gold_accounts) if gold_accounts else None,
        evidence_hits=evidence_hits,
        evidence_total=len(cited),
        field_hits=field_hits,
        positions=positions,
        lcs_ratio=lcs_length(pk, gk) / positions if positions else 1.0,
        order_agreement=pairwise_order_agreement(pk, gk),
        gold_operations=Counter(k[1] for k in gk),
        missing=sorted((gold_count - pred_count).elements()),
        extra=sorted((pred_count - gold_count).elements()),
    )


@dataclass
class Summary:
    cases: list[CaseScore]
    unscored: list[str]  # gold cases with no usable prediction row

    def __post_init__(self):
        self.n = len(self.cases)
        self.steps, self.struct, self.resources = PRF(), PRF(), PRF()
        self.by_phase = {p: PRF() for p in ("FINDING", "REMEDIATION", "VERIFICATION")}
        for c in self.cases:
            self.steps.add(c.steps)
            self.struct.add(c.struct)
            self.resources.add(c.resources)
            for phase, prf in c.by_phase.items():
                self.by_phase[phase].add(prf)

    def _mean(self, values) -> float:
        values = list(values)
        return sum(values) / len(values) if values else 1.0

    @property
    def status_accuracy(self) -> float:
        return self._mean(c.status_correct for c in self.cases)

    @property
    def exact_match_rate(self) -> float:
        return self._mean(c.exact for c in self.cases)

    @property
    def account_accuracy(self) -> float:
        return self._mean(c.account_correct for c in self.cases if c.account_correct is not None)

    @property
    def evidence_accuracy(self) -> float:
        total = sum(c.evidence_total for c in self.cases)
        return sum(c.evidence_hits for c in self.cases) / total if total else 1.0

    def field_accuracy(self, name: str) -> float:
        total = sum(c.positions for c in self.cases)
        return sum(c.field_hits[name] for c in self.cases) / total if total else 1.0

    @property
    def mean_field_accuracy(self) -> float:
        return self._mean(self.field_accuracy(n) for n in POSITION_FIELDS)

    @property
    def sequence_exact_rate(self) -> float:
        return self._mean(not c.missing and not c.extra and c.lcs_ratio == 1.0 for c in self.cases)

    @property
    def mean_lcs_ratio(self) -> float:
        return self._mean(c.lcs_ratio for c in self.cases)

    @property
    def mean_order_agreement(self) -> float:
        return self._mean(c.order_agreement for c in self.cases)

    @property
    def composite(self) -> float:
        """Local proxy only: equal weight on status, step F1, field accuracy and sequence."""
        return (self.status_accuracy + self.steps.f1 + self.mean_field_accuracy + self.mean_lcs_ratio) / 4

    def confusion(self) -> dict[tuple[str, str], int]:
        return Counter((c.gold_status, c.pred_status) for c in self.cases)

    def operation_recall(self) -> dict[str, tuple[int, int]]:
        """operation -> (gold steps predicted exactly, gold steps)."""
        totals, misses = Counter(), Counter()
        for c in self.cases:
            totals.update(c.gold_operations)
            misses.update(k[1] for k in c.missing)
        return {op: (totals[op] - misses[op], totals[op]) for op in totals}


def score(gold: dict[str, tuple[str, list]], pred: dict[str, tuple[str, list | None]]) -> Summary:
    cases, unscored = [], []
    for case_id, (gold_status, gold_steps) in gold.items():
        pred_status, pred_steps = pred.get(case_id, ("", None))
        if pred_steps is None or not isinstance(pred_steps, list):
            unscored.append(case_id)
            pred_steps = []
        pred_steps = [s for s in pred_steps if isinstance(s, dict)]
        cases.append(score_case(case_id, gold_status, gold_steps, pred_status, pred_steps))
    return Summary(cases, unscored)
