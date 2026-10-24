"""Decide whether a request text authorizes deletion.

Transparent rules decide the clear cases in several languages. Texts the rules
cannot decide go to the configured LLM (Groq by default) in one batched, cached
call; without an API key they fall back to "not a deletion request" and are
flagged as low confidence.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from . import config, paths
from .llm import LLMUnavailable, call_llm, extract_json, text_block

CACHE_PATH = paths.REPO_ROOT / "code" / "cache" / "intent_llm.json"

# Explicit statements that the person does NOT want deletion. Checked first,
# because non-deletion requests often mention deletion only to rule it out.
KEEP_PATTERNS = [
    r"not (asking|requesting) for (deletion|erasure)",
    r"not a request to (erase|delete)",
    r"\b(don'?t|do not) (delete|close|remove|erase)\b",
    r"\bkeep (the|my) (account|profile)\b",
    r"\bleave (the|my) (account|profile)",
    r"\bcontinue using\b",
    r"\bstill (use|need|want)\b",
    r"\bwithout removing\b",
    r"\bmarketing (lists|emails) only\b",
    r"\bstop (the )?promotional\b",
    r"\bno (elimin|borr)\w*",  # es
    r"\bne (supprimez|fermez|effacez) pas\b",  # fr
    r"\bnicht (löschen|schließen)\b",  # de
    r"\bnão (exclu|apag|encerr)\w*",  # pt
    r"削除しないで", r"삭제하지", r"不要删除",  # ja, ko, zh
]

DELETION_PATTERNS = [
    r"\bdelet", r"\beras", r"\bremov", r"\bclose\b", r"\bclosed\b", r"\bterminate\b",
    # Follow-ups asking to confirm an earlier deletion actually took effect.
    r"\b(is|are) gone\b", r"\bcannot identify me\b",
    r"suppression", r"supprim", r"effac",  # fr
    r"eliminaci", r"eliminar", r"borrar", r"supresi",  # es
    r"exclus", r"apagar", r"encerrar",  # pt
    r"lösch", r"schließ",  # de
    r"cancellare", r"cancellazione",  # it
    r"verwijder",  # nl
    r"usun",  # pl
    r"radera",  # sv
    r"удал",  # ru
    r"削除", r"退会", r"삭제", r"탈퇴", r"删除", r"注销",  # ja, ko, zh
]


@dataclass(frozen=True)
class Intent:
    authorizes_deletion: bool
    reason: str
    method: str = "rule"  # rule | llm | intake | default
    confident: bool = True


def classify_by_rules(text: str) -> Intent | None:
    """Rule decision, or None when the text gives no clear signal either way."""
    lowered = text.lower()
    for pattern in KEEP_PATTERNS:
        if re.search(pattern, lowered):
            return Intent(False, f"explicit non-deletion phrase /{pattern}/")
    for pattern in DELETION_PATTERNS:
        if re.search(pattern, lowered):
            return Intent(True, f"deletion phrase /{pattern}/")
    return None


def classify_intent(text: str) -> Intent:
    """Rules only; undecided texts default to non-deletion with low confidence."""
    return classify_by_rules(text) or Intent(False, "no deletion or keep phrase found", "default", confident=False)


LLM_PROMPT = """You review customer privacy requests for an online shop.
For each request decide whether it authorizes DELETION of the person's account or personal data
(erasure, closing the account permanently, removing their data, or asking to confirm that such a deletion is finished).
Requests for access, a copy or export of data, correction, changing an email/phone, login help,
or opting out of marketing are NOT deletion requests, even if they mention deletion.
Requests may be in any language.

Return ONLY JSON: {"results": [{"id": <id>, "authorizes_deletion": true|false, "reason": "<max 12 words>"}]}

Requests:
"""


def _key(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class IntentClassifier:
    """Rules first; undecided texts are sent to an LLM in batches and cached by text hash."""

    def __init__(self, use_llm: bool = True, model: str | None = None, provider: str | None = None, batch_size: int = 40, llm_call=None):
        self.use_llm = use_llm
        self.provider = provider or config.llm_provider()
        self.model = model or config.model_for(self.provider, "text")
        self.batch_size = batch_size
        self._llm_call = llm_call or self._call_model
        self.cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}

    def classify_all(self, texts: list[str]) -> dict[str, Intent]:
        results = {}
        undecided = []
        for text in dict.fromkeys(texts):
            decided = classify_by_rules(text)
            if decided:
                results[text] = decided
            else:
                undecided.append(text)
        llm = self.llm_judgements(undecided) if undecided and self.use_llm else {}
        for text in undecided:
            if text in llm:
                verdict = llm[text]
                results[text] = Intent(verdict["authorizes_deletion"], f"llm: {verdict.get('reason', '')}", "llm")
            else:
                results[text] = classify_intent(text)
        return results

    def llm_judgements(self, texts: list[str]) -> dict[str, dict]:
        """LLM verdicts for texts, using the cache; texts it cannot judge are omitted."""
        todo = [t for t in dict.fromkeys(texts) if _key(t) not in self.cache]
        for start in range(0, len(todo), self.batch_size):
            batch = todo[start : start + self.batch_size]
            try:
                verdicts = self._llm_call(batch)
            except LLMUnavailable:
                break
            for text, verdict in verdicts.items():
                self.cache[_key(text)] = {**verdict, "provider": self.provider, "model": self.model}
            self._save()
        return {t: self.cache[_key(t)] for t in texts if _key(t) in self.cache}

    def _call_model(self, batch: list[str]) -> dict[str, dict]:
        listing = "\n".join(json.dumps({"id": i, "text": t}, ensure_ascii=False) for i, t in enumerate(batch))
        reply = call_llm(
            [text_block(LLM_PROMPT + listing)],
            purpose="intent_classification",
            task="text",
            provider=self.provider,
            model=self.model,
            max_tokens=60 * len(batch) + 200,
            json_mode=True,
        )
        out = {}
        for item in extract_json(reply).get("results", []):
            i = item.get("id")
            if isinstance(i, int) and 0 <= i < len(batch) and isinstance(item.get("authorizes_deletion"), bool):
                out[batch[i]] = {"authorizes_deletion": item["authorizes_deletion"], "reason": str(item.get("reason", ""))[:120]}
        return out

    def _save(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(self.cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
