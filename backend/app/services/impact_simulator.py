"""
Change Impact Simulator.

Static-analysis-based component impact prediction:
  CONFIRMED  -- a direct textual/AST reference was found in the diff or repo
  INFERRED   -- a heuristic signal suggests a relationship (naming, imports,
                shared directory) but no direct reference was confirmed
  UNRESOLVED -- SecurePR could not determine whether the component is
                affected (e.g. dynamic dispatch, reflection, missing context)

This module is intentionally conservative: it is far better to mark
something UNRESOLVED than to silently assert a confidence level it cannot
support, since the whole point of this feature is honest, evidence-backed
prediction rather than a plausible-sounding guess.
"""
import re
from dataclasses import dataclass, field


@dataclass
class ImpactItem:
    component_type: str  # function|api|service|module|database|dependency|test
    component_name: str
    confidence: str       # CONFIRMED | INFERRED | UNRESOLVED
    evidence_summary: str
    potential_consequence: str


FUNC_DEF_RE = re.compile(r"^[+-]\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", re.MULTILINE)
CLASS_DEF_RE = re.compile(r"^[+-]\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.MULTILINE)
IMPORT_RE = re.compile(r"^[+-]\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)
ROUTE_RE = re.compile(r"@(?:app|router)\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)", re.MULTILINE)
SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|ALTER\s+TABLE|CREATE\s+TABLE)\b", re.IGNORECASE)


def analyze_file_patch(file_path: str, patch: str) -> list[ImpactItem]:
    items: list[ImpactItem] = []
    if not patch:
        return items

    for m in FUNC_DEF_RE.finditer(patch):
        items.append(ImpactItem(
            "function", m.group(1), "CONFIRMED",
            f"Function definition changed directly in diff of {file_path}.",
            "Callers of this function may receive different behavior or signature.",
        ))

    for m in CLASS_DEF_RE.finditer(patch):
        items.append(ImpactItem(
            "module", m.group(1), "CONFIRMED",
            f"Class definition changed directly in diff of {file_path}.",
            "Instances/subclasses of this class may be affected.",
        ))

    for m in ROUTE_RE.finditer(patch):
        items.append(ImpactItem(
            "api", f"{m.group(1).upper()} {m.group(2)}", "CONFIRMED",
            f"HTTP route handler changed in {file_path}.",
            "Any client calling this endpoint may see changed behavior, status codes, or payloads.",
        ))

    for m in IMPORT_RE.finditer(patch):
        dep = m.group(1) or m.group(2)
        if dep:
            items.append(ImpactItem(
                "dependency", dep, "INFERRED",
                f"Import statement for '{dep}' changed in {file_path}.",
                "Modules depending on this import path could be affected; not confirmed without call-graph analysis.",
            ))

    if SQL_RE.search(patch):
        items.append(ImpactItem(
            "database", f"SQL statement in {file_path}", "CONFIRMED",
            "Raw SQL statement present in the diff.",
            "Schema or data could be directly affected; verify against a staging database before merge.",
        ))

    if "test" in file_path.lower():
        items.append(ImpactItem(
            "test", file_path, "CONFIRMED",
            "Test file modified.",
            "Test coverage for the related feature is changing; verify assertions still match intended behavior.",
        ))

    return items


def infer_cross_file_relationships(changed_files: list[dict]) -> list[ImpactItem]:
    """
    Heuristic pass across the whole changeset: flags services/modules that
    share a directory with a changed file but whose own content wasn't
    changed -- these are plausible blast-radius candidates that cannot be
    confirmed from the diff alone.
    """
    items: list[ImpactItem] = []
    touched_dirs = {f["file_path"].rsplit("/", 1)[0] for f in changed_files if "/" in f["file_path"]}
    changed_paths = {f["file_path"] for f in changed_files}

    for d in touched_dirs:
        items.append(ImpactItem(
            "module", d, "UNRESOLVED",
            f"Directory '{d}' contains changed file(s) but sibling files were not individually diffed by this simulator.",
            "Other modules in this directory may share state or configuration; manual review recommended.",
        ))
    return items


def simulate(changed_files: list[dict]) -> list[ImpactItem]:
    results: list[ImpactItem] = []
    for f in changed_files:
        results.extend(analyze_file_patch(f["file_path"], f.get("patch_excerpt", "")))
    results.extend(infer_cross_file_relationships(changed_files))
    return results
