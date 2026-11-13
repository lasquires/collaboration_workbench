"""Pure, deterministic rules for matching research candidates to questions."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


_SCOPE_FIELDS = (
    "namespace", "scope_key", "artifact_version_id", "binary_build_id",
    "native_mapping_id", "type", "offset",
)


def canonical_semantic_fingerprint(payload: dict[str, Any]) -> str:
    """Hash canonical JSON semantics (stable across mapping/list presentation order)."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _criteria_semantics(groups: list[dict[str, Any]], allow_broad: bool) -> dict[str, Any]:
    unique = {json.dumps(g, sort_keys=True, separators=(",", ":"), ensure_ascii=False): g
              for g in groups}
    return {"any_of": [unique[k] for k in sorted(unique)],
            "allow_low_specificity": allow_broad}


def validate_criteria(criteria: dict, *, allow_empty: bool = False) -> dict:
    """One intake/execution contract; an empty object opts out of auto matching."""
    if allow_empty and criteria == {}:
        return criteria
    if not isinstance(criteria, dict) or not isinstance(criteria.get("any_of"), list) or not criteria["any_of"]:
        raise ValueError("criteria.any_of must be a non-empty list")
    if set(criteria) - {'any_of','allow_low_specificity'} or len(criteria['any_of'])>500:
        raise ValueError("Unsupported criteria fields or too many groups")
    allow_broad = criteria.get("allow_low_specificity", False)
    if not isinstance(allow_broad, bool):
        raise ValueError("allow_low_specificity must be boolean")
    raw_groups = criteria["any_of"]
    for group in raw_groups:
        if not isinstance(group, dict) or not isinstance(group.get("identifier"), str) or not group["identifier"]:
            raise ValueError("each criteria group requires an exact identifier")
        if set(group) - ({"identifier"} | set(_SCOPE_FIELDS)):
            raise ValueError("unsupported criteria constraint")
        for key, value in group.items():
            if (not isinstance(value, (str, int, float)) or isinstance(value, bool)
                    or (isinstance(value, float) and not math.isfinite(value))):
                raise ValueError(f"criteria {key} must be a scalar")
    return criteria


def match_question(question_revision: dict, candidate: dict, rule: dict) -> dict | None:
    """Apply exact-scoped/v1 matching. Criteria groups are OR; group fields are AND."""
    if not isinstance(question_revision, dict) or not isinstance(candidate, dict) or not isinstance(rule, dict):
        raise ValueError("question_revision, candidate and rule must be objects")
    criteria = validate_criteria(question_revision.get('criteria'))
    raw_groups = criteria['any_of']
    allow_broad = criteria.get('allow_low_specificity',False)
    if rule.get("id") != "exact-scoped" or not isinstance(rule.get("version"), str):
        raise ValueError("unsupported match rule")
    project = question_revision.get("project_id")
    if not project or not question_revision.get("id") or not question_revision.get("criteria_revision"):
        raise ValueError("question identity and criteria revision are required")
    if candidate.get("project_id") != project:
        return None
    if candidate.get("candidate_type") not in ("observation", "finding_revision"):
        raise ValueError("unsupported candidate_type")
    if not candidate.get("id") or not candidate.get("evidence_revision"):
        raise ValueError("candidate identity and evidence_revision are required")
    candidate_identifier = candidate.get("identifier")
    if not isinstance(candidate_identifier, str):
        return None
    matching_groups = [g for g in raw_groups
                       if (allow_broad or set(g) != {"identifier"})
                       and all(candidate.get(k) == v for k, v in g.items())
                       and candidate_identifier == g["identifier"]]
    if not matching_groups:
        return None
    matched_group = min(matching_groups, key=lambda g: json.dumps(
        g, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    broad = set(matched_group) == {"identifier"}
    compared = sorted(matched_group)
    scopes = {key: matched_group[key] for key in compared if key != "identifier"}
    hashes = candidate.get("support_hashes", [])
    if not isinstance(hashes, list) or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
        raise ValueError("support_hashes must be lowercase SHA-256 hashes")
    hashes = sorted(set(hashes))
    semantics = _criteria_semantics(raw_groups, allow_broad)
    fp = canonical_semantic_fingerprint({
        "project_id": project, "question_id": question_revision["id"],
        "criteria_semantics": semantics, "candidate_type": candidate["candidate_type"],
        "candidate_id": candidate["id"], "matched_fields": {k: candidate[k] for k in compared},
        "scope_constraints": scopes, "support_hashes": hashes,
    })
    return {
        "question_id": question_revision["id"], "criteria_revision": question_revision["criteria_revision"],
        "candidate_type": candidate["candidate_type"], "candidate_id": candidate["id"],
        "evidence_revision": candidate["evidence_revision"], "rule_id": rule["id"],
        "rule_version": rule["version"], "compared_fields": {k: candidate[k] for k in compared},
        "scope_constraints": scopes, "support_hashes": hashes,
        "reason": "Exact identifier and explicit scope constraints matched.",
        "fingerprint": fp, "low_specificity": broad,
    }
