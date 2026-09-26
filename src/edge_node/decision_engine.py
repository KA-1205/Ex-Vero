import re
from typing import List, Tuple, Dict, Any
from qdrant_edge import EdgeShard, Query, QueryRequest as EdgeQueryRequest, Point
from .adapter import Adapter

Verdict = str  # one of: "KEEP_LOCAL", "QUEUE_LOW", "QUEUE_HIGH", "REDACT_AND_QUEUE", "REJECT"


class DecisionEngine:
    def __init__(self, policy_config: Dict[str, Any]):
        self.policy = policy_config
        # compile PII regex patterns
        self.pii_patterns = [re.compile(p, re.IGNORECASE) for p in policy_config.get("pii_patterns", [])]
        self.urgency_keywords = set(k.lower() for k in policy_config.get("urgency_keywords", []))
        self.urgency_threshold = float(policy_config.get("urgency_threshold", 0.8))
        self.novelty_high = float(policy_config.get("novelty_threshold_high", 0.98))
        self.novelty_low = float(policy_config.get("novelty_threshold_low", 0.70))
        self.required_fields = set(policy_config.get("required_fields", []))
        self.bandwidth = policy_config.get("bandwidth", "FULL")  # OFFLINE, DEGRADED, FULL

    def _compute_urgency(self, text: str) -> float:
        """Return urgency score 0-1 based on keyword presence."""
        if not self.urgency_keywords:
            return 0.0
        lower_text = text.lower()
        matches = sum(1 for kw in self.urgency_keywords if kw in lower_text)
        # normalize by number of keywords? We'll just do min(1, matches / len(self.urgency_keywords))
        # but spec says lightweight rule/classifier; we'll keep simple: if any keyword present -> 1.0 else 0.0
        # However we need a score 0-1; we'll do proportion.
        if len(self.urgency_keywords) == 0:
            return 0.0
        score = matches / len(self.urgency_keywords)
        return min(1.0, score)

    def _check_pii(self, text: str) -> Tuple[bool, str]:
        """Return (has_pii, reason). If has_pii True, we may redact or keep local."""
        for pattern in self.pii_patterns:
            if pattern.search(text):
                return True, f"PII detected matching pattern {pattern.pattern}"
        return False, ""

    def _check_completeness(self, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """Check that required fields are present in payload."""
        missing = [f for f in self.required_fields if f not in payload]
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"
        return True, ""

    def _novelty_check(self, shard: EdgeShard, adapter: Adapter, vector: List[float]) -> Tuple[float, str]:
        """Query shard for nearest neighbor and return similarity score and reason."""
        if shard is None:
            return 0.0, "No shard to check novelty"
        try:
            query_obj = Query.Nearest(query=vector, using=adapter.name)
            edge_request = EdgeQueryRequest(limit=1, query=query_obj, with_vector=False, with_payload=False)
            results = shard.query(edge_request)
            if not results:
                return 0.0, "No prior points found (novel)"
            # similarity score is in results[0].score (cosine similarity)
            similarity = results[0].score
            return similarity, f"Novelty score {similarity:.3f}"
        except Exception as e:
            # If error due to dimension mismatch etc., treat as novel
            return 0.0, f"Error during novelty check: {e}"

    def evaluate(self, payload: Dict[str, Any], vector: List[float], shard: EdgeShard, adapter: Adapter) -> Tuple[Verdict, str]:
        """
        Run the decision pipeline and return (verdict, reason).
        """
        value = payload.get("value", "")
        # 1. PII filter
        has_pii, pii_reason = self._check_pii(value)
        if has_pii:
            # According to spec: anything sensitive is either stripped to an anonymized embedding or flagged local_only: true
            # We'll choose to keep local (not queue) and redact? We'll output REDACT_AND_QUEUE? Actually spec says:
            # Sensitivity/PII filter — regex/NER pass; anything sensitive is either stripped to an anonymized embedding
            # or flagged local_only: true and never queued at all.
            # So we should return KEEP_LOCAL (or maybe a special verdict?). We'll treat as KEEP_LOCAL with reason.
            return "KEEP_LOCAL", f"PII detected: {pii_reason} -> kept local only"

        # 2. Completeness gate
        complete, complete_reason = self._check_completeness(payload)
        if not complete:
            # If incomplete, we cannot queue; treat as KEEP_LOCAL? Spec: fact can't leave QUEUE_* state until required fields populated.
            # So we keep local until fields are filled.
            return "KEEP_LOCAL", f"Incomplete: {complete_reason}"

        # 3. Novelty check
        novelty_score, novelty_reason = self._novelty_check(shard, adapter, vector)
        if novelty_score >= self.novelty_high:
            return "REJECT", f"Redundant (similarity {novelty_score:.3f} >= {self.novelty_high})"
        # Note: low novelty score (< low threshold) means high novelty -> may increase urgency.

        # 4. Urgency score
        urgency = self._compute_urgency(value)
        urgency_reason = f"Urgency score {urgency:.2f}"

        # Determine verdict based on urgency and novelty and bandwidth
        # High urgency overrides everything -> QUEUE_HIGH
        if urgency >= self.urgency_threshold:
            return "QUEUE_HIGH", f"{urgency_reason} (>= {self.urgency_threshold})"

        # If not high urgency, consider novelty: low similarity (high novelty) may raise to QUEUE_LOW or QUEUE_HIGH?
        # Spec: <0.70 similarity -> treat as high-priority novel information.
        # We'll interpret that as: if novelty_score < self.novelty_low, treat as high priority -> QUEUE_HIGH? 
        # But we already checked urgency < threshold. We'll map:
        # novelty_score < self.novelty_low -> QUEUE_HIGH (novelty high)
        # novelty_score between low and high -> QUEUE_LOW
        # novelty_score >= self.novelty_high -> REJECT (already caught)
        if novelty_score < self.novelty_low:
            return "QUEUE_HIGH", f"High novelty (similarity {novelty_score:.3f} < {self.novelty_low})"
        elif novelty_score < self.novelty_high:
            return "QUEUE_LOW", f"Moderate novelty (similarity {novelty_score:.3f})"
        else:
            # Should not happen because REJECT caught >= high
            return "KEEP_LOCAL", f"Low novelty (similarity {novelty_score:.3f})"


def load_policy(config_path: str) -> Dict[str, Any]:
    import yaml
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    return config.get('policy', {})


# For testing: simple in-memory feed store
_decision_feed: List[Dict[str, Any]] = []


def log_decision(device_id: str, payload: Dict[str, Any], verdict: Verdict, reason: str):
    entry = {
        "device_id": device_id,
        "payload": payload,
        "verdict": verdict,
        "reason": reason,
        "timestamp": __import__('datetime').datetime.utcnow().isoformat() + "Z"
    }
    _decision_feed.append(entry)


def get_feed(device_id: str = None):
    if device_id is None:
        return list(_decision_feed)
    return [e for e in _decision_feed if e["device_id"] == device_id]


def clear_feed():
    _decision_feed.clear()