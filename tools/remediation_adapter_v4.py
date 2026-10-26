"""Restricted COPY adapter. Has no access to expected answers or execution results."""

import difflib
import hashlib
import json
import posixpath
import re
import shlex

VERSION = "docker-copy-literal-adapter.v1"
POLICY = {
    "version": VERSION,
    "operation": "add_docker_copy",
    "allowed_files": ["Dockerfile"],
    "default_position": "after_last_copy",
    "target_resolution": "literal existing Dockerfile even when target_known is false; record assistance",
    "syntax": "one-source literal COPY, shell/JSON form, optional --chown, quotes/whitespace",
    "conditions": "conditional applicability under the declared fixture environment; no natural-language entailment",
}


class PlanError(ValueError):
    def __init__(self, outcome, reason):
        super().__init__(reason)
        self.outcome = outcome


def path_literal(value, *, destination=False):
    if (
        not isinstance(value, str)
        or not value
        or re.search(r"[\\\x00-\x20$*?\[\]{};|&`<>:]", value)
    ):
        raise PlanError("unsupported_plan", "Only literal, non-glob COPY paths are supported")
    if ".." in value.split("/") or (value.startswith("/") and not destination):
        raise PlanError("unsupported_plan", "COPY source must stay within the fixture")
    result = posixpath.normpath(value)
    if (
        destination
        and result.startswith("/")
        and not (result == "/app" or result.startswith("/app/"))
    ):
        raise PlanError("unsupported_plan", "Absolute destinations outside /app are unsupported")
    if result in (".", "/app"):
        # Explicit entire-context copies are outside the v1 allowlist.
        raise PlanError("unsupported_plan", "Whole-context/root COPY is unsupported")
    if value.endswith("/"):
        result += "/"
    return result


def parse_copy(line):
    match = re.fullmatch(r"\s*COPY\s+(.+?)\s*", line, re.IGNORECASE)
    if not match:
        raise PlanError("unsupported_plan", "Only COPY instructions can be applied")
    body, owner = match[1], None
    flag = re.match(r"--chown=([^\s]+)\s+", body)
    if flag:
        owner = flag[1].strip("\"'")
        if not re.fullmatch(r"(?:node|\d+)(?::(?:node|\d+))?", owner):
            raise PlanError("unsupported_plan", "Unsupported ownership flag")
        body = body[flag.end() :]
    if body.startswith("--"):
        raise PlanError("unsupported_plan", "Unsupported COPY option")
    try:
        parts = json.loads(body) if body.startswith("[") else shlex.split(body, posix=True)
    except (ValueError, TypeError) as exc:
        raise PlanError("ambiguous_plan", "COPY syntax cannot be parsed") from exc
    if not isinstance(parts, list) or len(parts) != 2 or not all(isinstance(p, str) for p in parts):
        raise PlanError("unsupported_plan", "Exactly one source and one destination are required")
    source, target = path_literal(parts[0]), path_literal(parts[1], destination=True)
    canonical = "COPY " + (f"--chown={owner} " if owner else "")
    canonical += json.dumps([source, target], ensure_ascii=False)
    return {"source": source, "destination": target, "chown": owner, "instruction": canonical}


def insertion_index(change, lines):
    """Respect recognized explicit locations; never correct them using an oracle."""
    text = change.get("instruction", "")
    explicit = change.get("position")
    line_matches = re.findall(
        r"(?:Dockerfile\s*)?(\d+)\s*(?:줄|행)|\bline\s+(\d+)\b", text, re.IGNORECASE
    )
    anchors = re.findall(r"`(COPY\s+[^`]+)`", text, re.IGNORECASE)
    after = bool(re.search(r"뒤|다음|\bafter\b", text, re.IGNORECASE))
    before = bool(re.search(r"앞|\bbefore\b", text, re.IGNORECASE))
    if explicit is not None:
        if not isinstance(explicit, dict) or set(explicit) != {"side", "line"}:
            raise PlanError("ambiguous_plan", "Unsupported explicit insertion position")
        side, number = explicit["side"], explicit["line"]
        if (
            side not in ("before", "after")
            or type(number) is not int
            or not 1 <= number <= len(lines)
        ):
            raise PlanError("ambiguous_plan", "Invalid explicit insertion position")
        return number if side == "after" else number - 1, False
    if line_matches or anchors or after or before:
        if after == before or len(line_matches) > 1 or len(anchors) > 1:
            raise PlanError(
                "ambiguous_plan", "Explicit location cannot be interpreted unambiguously"
            )
        number = int(next(v for v in line_matches[0] if v)) if line_matches else None
        if number is not None and not 1 <= number <= len(lines):
            raise PlanError("ambiguous_plan", "Specified line does not exist")
        if anchors:
            occurrences = [
                i + 1 for i, line in enumerate(lines) if line.strip() == anchors[0].strip()
            ]
            if len(occurrences) != 1 or (number is not None and number != occurrences[0]):
                raise PlanError(
                    "ambiguous_plan", "Specified anchor and line do not match the input"
                )
            number = occurrences[0]
        if number is None:
            raise PlanError("ambiguous_plan", "Explicit relative location has no supported anchor")
        return number if after else number - 1, False
    copies = [i for i, line in enumerate(lines) if re.match(r"^\s*COPY\s", line, re.IGNORECASE)]
    if not copies:
        raise PlanError("ambiguous_plan", "Default COPY insertion anchor does not exist")
    return copies[-1] + 1, True


def adapt(plan, source_files, policy):
    if policy != POLICY:
        raise ValueError("Unsupported adapter policy/version")
    files = {f["path"]: f["content"] for f in source_files}
    if "Dockerfile" not in files:
        raise PlanError("ambiguous_plan", "Dockerfile not supplied")
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        raise PlanError("ambiguous_plan", "First plan has no changes")
    # Validate the entire plan before applying any part of it.
    original = files["Dockerfile"]
    lines, operations, assistance, edits = original.splitlines(), [], [], []
    for change in changes:
        target = change.get("target", "")
        if target in ("", None) or change.get("placeholders"):
            raise PlanError("ambiguous_plan", "Missing target or unresolved placeholders")
        if target.removeprefix("./") != "Dockerfile":
            raise PlanError("unsupported_plan", "Only the supplied Dockerfile may change")
        snippet = change.get("snippet", "").strip()
        if snippet.startswith("```"):
            parts = snippet.splitlines()
            if len(parts) < 3 or parts[-1] != "```":
                raise PlanError("ambiguous_plan", "Unclosed snippet fence")
            snippet = "\n".join(parts[1:-1])
        if not snippet or "{{" in snippet:
            raise PlanError("ambiguous_plan", "Empty or unresolved snippet")
        instructions = [parse_copy(line) for line in snippet.splitlines() if line.strip()]
        index, default = insertion_index(change, lines)
        edits.append((index, [i["instruction"] for i in instructions]))
        operations.extend(instructions)
        if default:
            assistance.append(
                {"field": "insertion_position", "value": index, "basis": "after_last_copy"}
            )
        if change.get("target_known") is False:
            assistance.append(
                {
                    "field": "target_existence",
                    "value": "Dockerfile",
                    "basis": "literal target in supplied fixture; model marked unknown",
                }
            )
    # Stable insertion order; all indices refer to the original file.
    result = []
    for i in range(len(lines) + 1):
        for index, added in edits:
            if index == i:
                result.extend(added)
        if i < len(lines):
            result.append(lines[i])
    modified = "\n".join(result) + "\n"
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(True), modified.splitlines(True), "a/Dockerfile", "b/Dockerfile"
        )
    )
    return {
        "dockerfile": modified,
        "operations": operations,
        "adapter_supplied_fields": assistance,
        "patch_diff": diff,
        "normalized_patch_sha256": hashlib.sha256(modified.encode()).hexdigest(),
    }
