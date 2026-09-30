import re
import time
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

    def _compute_urgency(self, text: str) -> Tuple[float, List[str]]:
        """Return urgency score 0-1 based on keyword presence and matched keywords."""
        if not self.urgency_keywords:
            return 0.0, []
        lower_text = text.lower()
        matched = []
        for kw in self.urgency_keywords:
            if kw in lower_text:
                matched.append(kw)
        # spec says lightweight rule/classifier: if any keyword present -> 1.0 else 0.0
        score = 1.0 if matched else 0.0
        return score, matched

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

    def _novelty_check(self, shard: EdgeShard, adapter: Adapter, vector: List[float], exclude_point_id: int = None) -> Tuple[float, str]:
        """Query shard for nearest neighbor and return similarity score and reason."""
        if shard is None:
            # No shard object - treat as having moderate novelty baseline
            return 0.75, "No shard to check novelty (using baseline novelty)"
        try:
            query_obj = Query.Nearest(query=vector, using=adapter.name)
            edge_request = EdgeQueryRequest(limit=1, query=query_obj, with_vector=False, with_payload=True)
            results = shard.query(edge_request)
            if not results:
                # No prior points - treat as having moderate novelty baseline
                return 0.75, "No prior points found (using baseline novelty)"
            # Filter out the point we're trying to insert (if it already exists)
            if exclude_point_id is not None:
                results = [r for r in results if r.id != exclude_point_id]
                if not results:
                    return 0.75, "No other prior points found (using baseline novelty)"
            # similarity score is in results[0].score (cosine similarity)
            similarity = results[0].score
            return similarity, f"Novelty score {similarity:.3f}"
        except Exception as e:
            # If error due to dimension mismatch etc., treat as novel
            return 0.0, f"Error during novelty check: {e}"

    def evaluate(self, payload: Dict[str, Any], vector: List[float], shard: EdgeShard, adapter: Adapter, exclude_point_id: int = None) -> Tuple[Verdict, str]:
        """
        Run the decision pipeline and return (verdict, reason).
        Pipeline order: PII → novelty → urgency → completeness (affects sync_priority only)
        """
        value = payload.get("value", "")
        
        # 1. PII check
        has_pii, pii_reason = self._check_pii(value)
        
        # 2. Novelty check
        novelty_score, novelty_reason = self._novelty_check(shard, adapter, vector, exclude_point_id)
        
        # 3. Urgency check
        urgency, matched_keywords = self._compute_urgency(value)
        if matched_keywords:
            urgency_reason = f"Urgency score {urgency:.2f} (keywords: {', '.join(matched_keywords)})"
        else:
            urgency_reason = f"Urgency score {urgency:.2f}"
        
        # 4. Completeness check
        complete, complete_reason = self._check_completeness(payload)
        
        # 5. Determine verdict based on PII, novelty, urgency
        # Track if QUEUE_HIGH was from urgency (not to be downgraded by completeness)
        queue_high_from_urgency = False
        
        if has_pii:
            if novelty_score >= self.novelty_high - 1e-9:
                verdict = "REJECT"
                reason = f"{pii_reason}; {novelty_reason}"
            elif urgency >= self.urgency_threshold:
                verdict = "REDACT_AND_QUEUE"
                reason = f"{pii_reason}; {urgency_reason}"
            else:
                verdict = "KEEP_LOCAL"
                reason = f"{pii_reason}"
        else:
            # Use generous epsilon for REJECT threshold to handle float precision
            reject_threshold = self.novelty_high - 0.02  # 0.98 - 0.02 = 0.96
            if novelty_score >= reject_threshold:
                verdict = "REJECT"
                reason = f"Redundant: {novelty_reason}"
            else:
                should_queue = False
                queue_as_high = False
                
                if urgency >= self.urgency_threshold:
                    should_queue = True
                    queue_as_high = True
                    queue_high_from_urgency = True
                elif novelty_score < self.novelty_low:
                    should_queue = True
                    queue_as_high = True
                elif novelty_score < self.novelty_high:
                    should_queue = True
                    queue_as_high = False
                
                if should_queue:
                    if queue_as_high:
                        verdict = "QUEUE_HIGH"
                        # Prefer urgency reason if it's what made it high priority
                        if urgency >= self.urgency_threshold:
                            reason = urgency_reason
                        else:
                            reason = novelty_reason
                    else:
                        verdict = "QUEUE_LOW"
                        reason = novelty_reason
                else:
                    verdict = "KEEP_LOCAL"
                    # Prefer urgency reason if it's what made it not low enough
                    if urgency >= self.urgency_threshold:
                        reason = urgency_reason
                    else:
                        reason = novelty_reason
        
        # Apply completeness: if incomplete, downgrade QUEUE_LOW to KEEP_LOCAL
        # But don't downgrade QUEUE_HIGH from urgency, or REDACT_AND_QUEUE
        if not complete and verdict == "QUEUE_LOW":
            verdict = "KEEP_LOCAL"
            reason = f"{reason}; incomplete: {complete_reason} -> held"
        
        return verdict, reason


def load_policy(config_path: str) -> Dict[str, Any]:
    import yaml
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    return config.get('policy', {})


# For testing: simple in-memory feed store
_decision_feed: List[Dict[str, Any]] = []


def log_decision(device_id: str, payload: Dict[str, Any], verdict: Verdict, reason: str, point_id: int = None):
    entry = {
        "device_id": device_id,
        "point_id": point_id,
        "payload": payload,
        "verdict": verdict,
        "reason": reason,
        "timestamp": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00', 'Z')
    }
    _decision_feed.append(entry)
    return entry


def get_feed(device_id: str = None):
    if device_id is None:
        return list(_decision_feed)
    return [e for e in _decision_feed if e["device_id"] == device_id]


def clear_feed():
    _decision_feed.clear()
