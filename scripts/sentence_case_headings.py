#!/usr/bin/env python3
"""Convert the headings Vale flags (Microsoft.Headings) to sentence case.

Usage:
    python scripts/sentence_case_headings.py [PATH ...]          # preview changes
    python scripts/sentence_case_headings.py [PATH ...] --write  # apply them

PATH defaults to docs. Words that are acronyms (RAG, UX), contain digits (P35) or
have capitals after the first letter (TeamView, OURdata) are left alone. Names
that should stay capitalised go in scripts/heading-terms.txt, one per line.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

RULE = "Microsoft.Headings"
TERMS_FILE = Path(__file__).with_name("heading-terms.txt")
OPEN_PUNCT = "‘“'\"(["


def load_terms():
    if not TERMS_FILE.is_file():
        return []
    lines = TERMS_FILE.read_text(encoding="utf-8").splitlines()
    terms = [t.strip() for t in lines if t.strip() and not t.startswith("#")]
    # Longest first, so "Tagomi Commerce" wins over "Tagomi".
    return sorted(terms, key=len, reverse=True)


def keep_case(part):
    return bool(re.search(r"\d", part)) or part == "I" or any(c.isupper() for c in part[1:])


def fix_word(word, capitalise):
    # Split on hyphens and slashes but keep them, so each part is cased on its own.
    parts = re.split(r"([-/])", word)
    word = "".join(p if p in "-/" or keep_case(p.lstrip(OPEN_PUNCT)) else p.lower() for p in parts)
    if capitalise:
        i = len(word) - len(word.lstrip(OPEN_PUNCT))
        word = word[:i] + word[i:i + 1].upper() + word[i + 1:]
    return word


def sentence_case(heading, terms):
    words, capitalise = [], True
    for word in heading.split(" "):
        words.append(fix_word(word, capitalise) if word else word)
        capitalise = word.endswith(":")
    result = " ".join(words)
    for term in terms:
        result = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", term, result, flags=re.I)
    return result


def flagged_headings(paths):
    proc = subprocess.run(
        ["vale", "--output=JSON", "--filter", f'.Name=="{RULE}"', *paths],
        capture_output=True, text=True, encoding="utf-8",
    )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        sys.exit(f"error: could not read Vale output:\n{proc.stdout}{proc.stderr}")


def main():
    parser = argparse.ArgumentParser(description="Sentence-case the headings Vale flags.")
    parser.add_argument("paths", nargs="*", default=["docs"], help="files or folders to check (default: docs)")
    parser.add_argument("--write", action="store_true", help="apply the changes (default: preview only)")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    terms = load_terms()
    changed = 0
    for path, alerts in flagged_headings(args.paths).items():
        file = Path(path)
        text = file.read_text(encoding="utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline)
        for alert in alerts:
            line_no, old = alert["Line"], alert["Match"]
            new = sentence_case(old, terms)
            if new == old:
                print(f"{path}:{line_no}: '{old}' already OK (Vale doesn't recognise a name in it)")
                continue
            if old not in lines[line_no - 1]:
                print(f"{path}:{line_no}: skipped, heading text not found on that line")
                continue
            lines[line_no - 1] = lines[line_no - 1].replace(old, new, 1)
            changed += 1
            print(f"{path}:{line_no}: '{old}' -> '{new}'")
        if args.write:
            file.write_text(newline.join(lines), encoding="utf-8", newline="")

    verb = "Changed" if args.write else "Would change"
    print(f"\n{verb} {changed} heading(s).{'' if args.write else ' Run with --write to apply.'}")


if __name__ == "__main__":
    main()
