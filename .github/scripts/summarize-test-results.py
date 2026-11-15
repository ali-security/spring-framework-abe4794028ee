#!/usr/bin/env python3
"""Print per-module JUnit results and every skipped test with its reason.

Gradle's JUnit XML leaves <skipped/> without a message when a test is disabled
by a JUnit condition (@Disabled, @EnabledForTestGroups, @DisabledForJreRange,
@IfProfileValue, ...). For those, the reason is recovered from the condition
annotations declared on the test method, its class, or a superclass.
"""
import glob
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

CONDITION = re.compile(
    r"^\s*@(?!DisabledInAotMode)(Disabled\w*|Enabled\w*|Ignore|IfProfileValue|Assume\w*)\b.*$"
)
ASSUME = re.compile(r"\bassume\w*\s*\(|Assumptions\.\w+\s*\(")
_cache = {}


def find_source(module, top_class):
    """Source file for a fully qualified top-level class (or a bare simple name)."""
    key = (module, top_class)
    if key not in _cache:
        rel = top_class.replace(".", "/")
        pattern = f"{module}/src/*/*/{rel}" if "." in top_class else f"{module}/src/*/*/**/{rel}"
        found = None
        for ext in ("java", "kt", "groovy"):
            hits = glob.glob(f"{pattern}.{ext}", recursive=True)
            if hits:
                found = hits[0]
                break
        _cache[key] = found
    return _cache[key]


def read_lines(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def annotations_before(lines, idx):
    """Condition annotations in the contiguous annotation/comment block above lines[idx]."""
    found = []
    j = idx - 1
    while j >= 0:
        s = lines[j].strip()
        if not s or s.endswith((";", "{", "}")) and not s.startswith("@"):
            break
        if CONDITION.match(lines[j]):
            found.append(s)
        j -= 1
    return list(reversed(found))


def declaration_index(lines, pattern):
    rx = re.compile(pattern)
    for i, line in enumerate(lines):
        if rx.search(line) and not line.lstrip().startswith(("*", "//", "/*")):
            return i
    return None


def body_assumptions(lines, idx):
    """assume*(...) calls inside the brace-delimited body starting at lines[idx]."""
    depth, opened, found = 0, False, []
    for line in lines[idx:]:
        if ASSUME.search(line):
            found.append(" ".join(line.split()))
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            break
    return found


def resolve(module, classname):
    """(path, lines, simple name, declaration index, package) for a test class name."""
    classname = re.sub(r"^Gradle Test Run \S+\.", "", classname)
    top, _, nested = classname.partition("$")
    path = find_source(module, top)
    if not path:
        return None
    lines = read_lines(path)
    simple = nested.split("$")[-1] if nested else top.split(".")[-1]
    idx = declaration_index(lines, rf"\b(class|interface)\s+{re.escape(simple)}\b")
    pkg_line = next((l for l in lines if l.startswith("package ")), "package ;")
    pkg = pkg_line[len("package "):].strip().rstrip(";")
    return path, lines, simple, idx, pkg


def superclass(module, info):
    path, lines, simple, idx, pkg = info
    if idx is None:
        return None
    m = re.search(r"\bextends\s+([\w.]+)", " ".join(lines[idx:idx + 3]))
    if not m:
        return None
    name = m.group(1)
    if "." not in name:
        imp = next((l for l in lines if re.match(rf"import\s+[\w.]+\.{name};", l)), None)
        name = imp.split()[1].rstrip(";") if imp else f"{pkg}.{name}"
    return name


def hierarchy(module, classname):
    seen, info = 0, resolve(module, classname)
    while info and seen < 5:
        yield info
        parent = superclass(module, info)
        info = resolve(module, parent) if parent else None
        seen += 1


def source_reasons(module, classname, method):
    reasons, assumptions, method_found = [], [], False
    for path, lines, simple, idx, pkg in hierarchy(module, classname):
        if not method_found and method:
            m_idx = declaration_index(lines, rf"\b(void|fun)\s+`?{re.escape(method)}`?\s*\(")
            if m_idx is not None:
                method_found = True
                reasons += [f"method {simple}.{method}: {a}" for a in annotations_before(lines, m_idx)]
                assumptions += body_assumptions(lines, m_idx)
        if idx is not None:
            reasons += [f"class {simple}: {a}" for a in annotations_before(lines, idx)]
        if not assumptions:
            assumptions += [" ".join(l.split()) for l in lines
                            if ASSUME.search(l) and not l.lstrip().startswith(("import", "*", "//"))][:3]
    return reasons, assumptions


def reason_for(module, classname, name, message):
    method = re.split(r"[(\[]", name, 1)[0].strip()
    reasons, assumptions = source_reasons(module, classname, method)
    parts = []
    if message:
        parts.append(" ".join(message.split()))
    if reasons:
        parts.append("disabled by JUnit condition (from source): " + "; ".join(reasons))
    if assumptions and (not reasons or "Assum" in (message or "")):
        parts.append("assumption in the test source: " + "; ".join(assumptions[:3]))
    if not parts:
        parts.append("no message recorded in the JUnit XML and no condition annotation found in source")
    return " | ".join(parts)


def main():
    totals = defaultdict(lambda: [0, 0, 0, 0])
    skipped = []
    for f in sorted(glob.glob("**/build/test-results/**/TEST-*.xml", recursive=True)):
        module = f.split("/build/test-results/")[0]
        root = ET.parse(f).getroot()
        t = totals[module]
        t[0] += int(root.get("tests", 0))
        t[1] += int(root.get("failures", 0))
        t[2] += int(root.get("errors", 0))
        t[3] += int(root.get("skipped", 0))
        for tc in root.iter("testcase"):
            s = tc.find("skipped")
            if s is not None:
                cls, name = tc.get("classname"), tc.get("name")
                skipped.append(f"{module}: {cls}.{name} -- {reason_for(module, cls, name, s.get('message'))}")
    print("Module | tests | passed | failures | errors | skipped")
    for m in sorted(totals):
        t, fl, e, sk = totals[m]
        print(f"{m} | {t} | {t - fl - e - sk} | {fl} | {e} | {sk}")
    T = [sum(v[i] for v in totals.values()) for i in range(4)]
    print(f"TOTAL: {T[0]} tests, {T[0] - T[1] - T[2] - T[3]} passed, {T[1]} failures, {T[2]} errors, {T[3]} skipped")
    print(f"Skipped tests ({len(skipped)}), each with its reason:")
    for line in skipped:
        print("  " + line)
    print("Notes: @EnabledForTestGroups(LONG_RUNNING) / TestGroup.LONG_RUNNING tests run only when the build is")
    print("invoked with -PtestGroups=long_running (buildSrc TestConventions); upstream CI does not pass it.")
    print("@EnabledIfRuntimeHintsAgent tests run only in the dedicated runtimeHintsTest task with the agent attached.")


if __name__ == "__main__":
    main()
