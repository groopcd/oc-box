"""Check private scanner reports and print a redacted GitHub Markdown summary."""

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import stat


MAX_REPORT_BYTES = 64 * 1024 * 1024
# Deliberately partial; categories are sorted and only these labels may print.
OWASP_CWES = {
    "A01:2025 Broken Access Control": {22, 284, 285, 352, 862, 863},
    "A04:2025 Cryptographic Failures": {326, 327, 328, 330},
    "A05:2025 Injection": {78, 79, 89, 94},
    "A07:2025 Authentication Failures": {287, 306},
    "A08:2025 Software or Data Integrity Failures": {502},
}
PYTHON_OWASP = "A03:2025 Software Supply Chain Failures"


class InvalidReport(ValueError):
    """A fixed, safe diagnostic; never include an input value."""


def _need(condition, message="Unexpected report structure."):
    if not condition:
        raise InvalidReport(message)


def _object(value):
    _need(isinstance(value, dict))
    return value


def _array(value):
    _need(isinstance(value, list))
    return value


def _text(value):
    _need(isinstance(value, str) and bool(value.strip()))
    return value


def _index(value):
    _need(type(value) is int and value >= -1)
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, "Duplicate JSON keys.")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise InvalidReport("Invalid JSON number.")


def _read_report(path):
    try:
        _need(stat.S_ISREG(path.lstat().st_mode), "Expected a regular report file.")
        with path.open("rb") as report:
            data = report.read(MAX_REPORT_BYTES + 1)
        _need(len(data) <= MAX_REPORT_BYTES, "Report exceeds the size limit.")
        return _object(
            json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_json_object,
                parse_constant=_invalid_constant,
                parse_float=Decimal,
            )
        )
    except InvalidReport:
        raise
    except (OSError, ValueError, RecursionError, InvalidOperation):
        raise InvalidReport("Cannot read a complete UTF-8 JSON report.") from None


def _severity(properties):
    score = properties.get("security-severity")
    if isinstance(score, str):
        if not re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", score
        ):
            return None
    elif type(score) not in (int, float, Decimal):
        return None
    try:
        score = Decimal(str(score)) if isinstance(score, float) else Decimal(score)
    except InvalidOperation:
        return None
    if not score.is_finite() or not 0 <= score <= 10:
        return None
    return score


def _associations(properties):
    tags = _array(properties.get("tags", []))
    cwes = set()
    for tag in tags:
        match = re.fullmatch(
            r"external/cwe/cwe-([0-9]{1,4})", _text(tag), re.I | re.ASCII
        )
        if match:
            cwes.add(int(match[1]))
    return {name for name, selected in OWASP_CWES.items() if cwes & selected}


def _component_rules(component):
    rules = {}
    for index, rule in enumerate(_array(component.get("rules", []))):
        rule = _object(rule)
        rule_id = _text(rule.get("id"))
        _need(rule_id not in rules, "Ambiguous CodeQL rule identity.")
        properties = _object(rule.get("properties", {}))
        rules[rule_id] = (
            index,
            _text(rule["guid"]) if "guid" in rule else None,
            _severity(properties),
            _associations(properties),
        )
    return rules


def _resolve_rule(result, components, rules):
    reference = _object(result.get("rule", {}))
    identities = {
        _text(obj[key])
        for obj, key in ((result, "ruleId"), (reference, "id"))
        if key in obj
    }
    _need(len(identities) == 1, "Missing or conflicting CodeQL rule identity.")
    rule_id = identities.pop()
    indices = {
        _index(obj[key])
        for obj, key in ((result, "ruleIndex"), (reference, "index"))
        if key in obj
    } - {-1}
    _need(len(indices) <= 1, "Conflicting CodeQL rule index.")
    index = next(iter(indices), None)
    matches = [i for i, component in enumerate(rules) if rule_id in component]
    if "toolComponent" in reference:
        component = _object(reference["toolComponent"])
        extension_index = _index(component.get("index", -1))
        keys = [key for key in ("name", "guid") if key in component]
        for key in keys:
            _text(component[key])
        _need(extension_index != -1 or keys, "Missing CodeQL rule component identity.")
        matches = [
            i
            for i in matches
            if (extension_index == -1 or extension_index == i - 1)
            and all(component[key] == components[i].get(key) for key in keys)
        ]
    elif index is not None and 0 in matches and rules[0][rule_id][0] == index:
        # A SARIF index without a component reference defaults to the driver.
        matches = [0]
    _need(len(matches) == 1, "Unresolved or ambiguous CodeQL rule identity.")
    ordinal, guid, score, associations = rules[matches[0]][rule_id]
    _need(index is None or index == ordinal, "CodeQL rule index does not match its ID.")
    if "guid" in reference:
        _need(
            _text(reference["guid"]) == guid, "CodeQL rule GUID does not match its ID."
        )
    return score, associations


def parse_codeql(report):
    """Count SARIF findings by gate severity and selected CWE associations."""
    report = _object(report)
    runs = _array(report.get("runs"))
    _need(
        report.get("version") == "2.1.0" and runs, "Expected a CodeQL SARIF 2.1.0 run."
    )
    severities, associations = Counter(), Counter()
    for run in runs:
        run = _object(run)
        tool = _object(run.get("tool"))
        driver = _object(tool.get("driver"))
        _need(driver.get("name") == "CodeQL", "Unexpected SARIF scanner.")
        components = [driver] + [
            _object(item) for item in _array(tool.get("extensions", []))
        ]
        rules = [_component_rules(component) for component in components]
        for invocation in _array(run.get("invocations", [])):
            invocation = _object(invocation)
            _need(
                invocation.get("executionSuccessful") is True,
                "CodeQL reported an unsuccessful invocation.",
            )
            for key in ("toolExecutionNotifications", "toolConfigurationNotifications"):
                for notification in _array(invocation.get(key, [])):
                    _need(
                        _object(notification).get("level") != "error",
                        "CodeQL reported an invocation error.",
                    )
        for result in _array(run.get("results")):
            result = _object(result)
            message = _object(result.get("message"))
            _need(
                any(
                    isinstance(message.get(key), str) and message[key].strip()
                    for key in ("text", "markdown", "id")
                )
            )
            score, categories = _resolve_rule(result, components, rules)
            # Suppressions and baselines are not release-policy exceptions.
            bucket = "unknown" if score is None else "high" if score >= 7 else "lower"
            severities[bucket] += 1
            associations.update(categories or {"Unmapped"})
    return severities, associations


def parse_python(report, scanner_exit):
    """Count dependencies and unresolved advisory rows from pip-audit JSON."""
    _need(
        type(scanner_exit) is int and scanner_exit in (0, 1),
        "Missing or unsupported pip-audit exit status.",
    )
    report = _object(report)
    _need(
        report.keys() <= {"dependencies", "fixes"} and report.get("fixes", []) == [],
        "Unexpected pip-audit report metadata or remediation results.",
    )
    dependencies = _array(report.get("dependencies"))
    _need(dependencies, "pip-audit reported no dependencies for the installed scope.")
    advisories = 0
    for dependency in dependencies:
        dependency = _object(dependency)
        _need(
            "skip_reason" not in dependency,
            "pip-audit skipped at least one dependency.",
        )
        _need(set(dependency) == {"name", "version", "vulns"})
        _text(dependency.get("name"))
        _text(dependency.get("version"))
        for advisory in _array(dependency.get("vulns")):
            _text(_object(advisory).get("id"))
            advisories += 1
    _need(
        bool(advisories) == bool(scanner_exit),
        "pip-audit exit status disagrees with its findings.",
    )
    return len(dependencies), advisories


def _python_advisory_ids(report):
    """List only normalized package names and well-known public advisory IDs."""
    visible = set()
    for dependency in report["dependencies"]:
        package = dependency["name"].lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", package):
            continue
        for advisory in dependency["vulns"]:
            identifier = advisory["id"]
            if re.fullmatch(
                r"(?:CVE-[0-9]{4}-[0-9]{4,12}|PYSEC-[0-9]{4}-[0-9]{1,12}|"
                r"GHSA-(?:[23456789cfghjmpqrvwx]{4}-){2}[23456789cfghjmpqrvwx]{4})",
                identifier,
            ):
                visible.add((package, identifier))
    return sorted(visible)


def _summarize_codeql(path):
    details = [
        "- Scope: CodeQL static source analysis of supplied SARIF only; "
        "not runtime or dependency coverage."
    ]
    _need(path.suffix == ".sarif", "Expected one SARIF file.")
    total, mapped = parse_codeql(_read_report(path))
    details.extend(
        [
            f"- Findings: {sum(total.values())}; security severity ≥7: {total['high']}; "
            f"below 7: {total['lower']}; unknown: {total['unknown']}.",
            "- Gate policy: severity ≥7 or unknown blocks; suppressions do not exempt findings.",
            "- OWASP Top 10 (2025): selected CWE-tag associations (counts can overlap).",
        ]
    )
    details.extend(f"  - {label}: {mapped[label]}." for label in sorted(mapped))
    if not mapped:
        details.append("  - No findings to map.")
    return total["high"] + total["unknown"], details


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid security scan checker arguments. See --help.\n")


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("kind", choices=("codeql", "python"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--gate", action="store_true", help="block on policy findings")
    parser.add_argument(
        "--scanner-exit", type=int, help="required for Python (pip-audit status)"
    )
    parser.add_argument("--commit", required=True, help="full 40-hex scanned commit ID")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-fA-F0-9]{40}", args.commit):
        parser.error("invalid commit")

    mode = "Release gate" if args.gate else "Report only"
    scanner = "CodeQL" if args.kind == "codeql" else "pip-audit"
    print(
        f"### Security scan: {scanner}\n\n- Commit: `{args.commit}`.\n- Mode: {mode}."
    )
    try:
        if args.kind == "codeql":
            _need(
                args.scanner_exit is None, "Scanner exit status is only for pip-audit."
            )
            blockers, details = _summarize_codeql(args.path)
        else:
            report = _read_report(args.path)
            dependencies, blockers = parse_python(report, args.scanner_exit)
            details = [
                "- Scope: installed Python distributions in the audited environment; "
                "not source analysis or all possible dependency versions.",
                f"- Dependency rows: {dependencies}; unresolved advisory rows: {blockers}.",
                "- Gate policy: any unresolved advisory blocks; "
                "pip-audit does not supply a reliable severity for this gate.",
                f"- OWASP Top 10 (2025) scope association: {PYTHON_OWASP}.",
            ]
            identifiers = _python_advisory_ids(report)
            if identifiers:
                details.append("- Public advisory IDs (first 50):")
                details.extend(
                    f"  - `{package}`: `{identifier}`"
                    for package, identifier in identifiers[:50]
                )
    except (InvalidReport, OSError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, InvalidReport)
            else "Cannot open scanner evidence."
        )
        print(f"- Status: **Invalid scanner evidence.** {reason}")
        return 2
    print(*details, sep="\n")
    print(
        "- Counts are limited scanner observations, not OWASP compliance or security assurance."
    )
    if blockers:
        decision = (
            "Blocked by policy"
            if args.gate
            else "Policy findings reported; not enforced"
        )
        print(f"- Status: **{decision}.**")
        return 1 if args.gate else 0
    print("- Status: **No policy blockers.**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
