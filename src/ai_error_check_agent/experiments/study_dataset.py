"""Research split identities and reviewer-only references; never used for retrieval."""

import re
from urllib.parse import urlsplit

from ..source_contracts import safe_source_path
from .design import ROOT, digest
from .study import read


def repository_name(value):
    value = value.strip().rstrip("/").removesuffix(".git")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment:
            raise ValueError("Expected a canonical GitHub repository")
        value = parsed.path.strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        raise ValueError("Invalid repository identity")
    return value.lower()


def canonical_identity(case):
    identity = {
        "repository": repository_name(case["repository"]),
        "failed_commit": case["states"]["failed"]["commit"].lower(),
        "fixed_commit": case["states"]["fixed"]["commit"].lower(),
        "selected_test": case["test_full_title"],
    }
    if (
        any(
            not re.fullmatch(r"[0-9a-f]{40}", identity[k])
            for k in ("failed_commit", "fixed_commit")
        )
        or not identity["selected_test"].strip()
    ):
        raise ValueError("Incomplete canonical identity")
    return identity


def independent_key(identity):
    return digest([repository_name(identity["repository"]), identity["fixed_commit"].lower()])


def validate_split(selection, ledger, role):
    known_ids = set(ledger["development_bug_ids"])
    known_ids.update(f"bugsjs-express-{n}" for n in range(1, 11))
    known_ids.update(f"bugsjs-hessian-{n}" for n in (3, 5, 9))
    known = list(ledger.get("canonical_identities", []))
    # Also pin the original development identities independently of the supplied ledger.
    baseline = read(ROOT / "evaluation/public/study-development-identities.v3.json")
    known += baseline["canonical_identities"]
    known_keys = {independent_key(c) for c in known}
    known_sources = {(repository_name(c["repository"]), c["failed_commit"].lower()) for c in known}
    known_sources.update(
        (repository_name(c["repository"]), c["failed_commit"].lower())
        for c in baseline.get("known_sources", []) + ledger.get("known_sources", [])
    )
    ids, keys, identities = set(), set(), {}
    for c in selection["cases"]:
        identity = canonical_identity(c)
        key = independent_key(identity)
        if c["id"] in ids or key in keys:
            raise ValueError("Duplicate independent bug or fixed commit")
        if role == "evaluation" and (
            c["id"] in known_ids
            or key in known_keys
            or (identity["repository"], identity["failed_commit"]) in known_sources
        ):
            raise ValueError("Previously inspected canonical bug cannot be evaluation")
        ids.add(c["id"])
        keys.add(key)
        identities[c["id"]] = {
            "canonical_identity": identity,
            "independent_key": key,
            "project": identity["repository"],
        }
    if len(ids) != selection["required_bugs"]:
        raise ValueError("Selection count does not match required_bugs")
    return identities


def validate_references(document, identities, evaluation=False):
    refs = {}
    for reference in document["items"]:
        bug = reference["bug_id"]
        if bug in refs or bug not in identities:
            raise ValueError("Duplicate or unknown reference bug")
        if reference["canonical_identity"] != identities[bug]["canonical_identity"]:
            raise ValueError("Reference canonical identity mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", reference["reference_patch_sha256"]):
            raise ValueError("Invalid reference patch hash")
        claims = reference["essential_claims"]
        if len({c["id"] for c in claims}) != len(claims) or any(
            not c["id"] or not c["description"].strip() for c in claims
        ):
            raise ValueError("Invalid essential claims")
        for region in reference["acceptable_root_regions"] + [
            r for c in claims for r in c["support_regions"]
        ]:
            safe_source_path(region["path"])
            if (
                type(region["start_line"]) is not int
                or type(region["end_line"]) is not int
                or not 1 <= region["start_line"] <= region["end_line"]
            ):
                raise ValueError("Invalid reference region")
        reviewers = reference["reference_reviewers"]
        human_ids = {
            r["reviewer_id"].strip().casefold()
            for r in reviewers
            if r["reviewer_type"] == "human" and r["reviewer_id"].strip()
        }
        approved = (
            reference["reference_status"] == "approved"
            and len(human_ids) >= 2
            and bool(claims)
            and all(c["support_regions"] for c in claims)
            and bool(reference["acceptable_root_regions"])
            and bool(reference["acceptable_next_checks"])
        )
        if evaluation and not approved:
            raise ValueError(
                "Evaluation references require two human approvals and complete claims/regions/next checks"
            )
        refs[bug] = reference
    if evaluation and set(refs) != set(identities):
        raise ValueError("Evaluation references missing")
    return refs
