#!/usr/bin/env python3
"""Check that the KB routing map and the knowledge base agree.

The bot picks knowledge base files from a hardcoded map in
``api/process_email.py``. The files themselves live in a separate repo. Nothing
connects the two, so renaming or adding a KB file silently breaks routing --
which is exactly what happened when Beamer moved out of ``other_products.md``
into its own file and every Beamer email started being answered with no Beamer
knowledge at all.

This script fails on two kinds of drift:

* a routed filename that does not exist in the knowledge base
* a knowledge base file that no route can ever reach

The routing map is read straight out of the source with ``ast`` rather than by
importing it, so this runs without the bot's dependencies installed.

Usage::

    python scripts/check_kb_routing.py                    # fetch from GitHub
    python scripts/check_kb_routing.py --local ../Softorino_Support_AI
"""

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "api" / "process_email.py"
CONTENTS_API = (
    "https://api.github.com/repos/andrewqa78/Softorino_Support_AI/contents/knowledge_base"
)


def routing_from_source(path):
    """Pull base_files, the routing map and the fallback out of the source.

    Returns the set of every filename the bot can ever ask for.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "relevant_kb_files":
            break
    else:
        raise SystemExit(f"{path}: relevant_kb_files() not found -- did it get renamed?")

    referenced = set()
    for child in ast.walk(node):
        # base_files = [...] and routing = {...}
        if isinstance(child, ast.Assign):
            try:
                value = ast.literal_eval(child.value)
            except ValueError:
                continue
            if isinstance(value, list):
                referenced.update(v for v in value if isinstance(v, str))
            elif isinstance(value, dict):
                referenced.update(k for k in value if isinstance(k, str))
        # files.append("Softorino_Products.md") -- the no-keyword-matched fallback
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr == "append" and len(child.args) == 1:
                try:
                    arg = ast.literal_eval(child.args[0])
                except ValueError:
                    continue
                if isinstance(arg, str):
                    referenced.add(arg)

    if not referenced:
        raise SystemExit(f"{path}: no KB filenames found -- the parser needs updating.")
    return referenced


def kb_files_local(root):
    directory = Path(root).expanduser() / "knowledge_base"
    if not directory.is_dir():
        raise SystemExit(f"{directory} is not a directory.")
    return {p.name for p in directory.glob("*.md")}


def kb_files_github():
    headers = {"User-Agent": "Softorino-Email-Bot", "Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(CONTENTS_API, headers=headers), timeout=15) as response:
            entries = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        hint = " The KB repo is private -- set GITHUB_TOKEN." if exc.code in (403, 404) else ""
        raise SystemExit(f"GitHub returned {exc.code} for the KB listing.{hint}")
    except URLError as exc:
        raise SystemExit(f"Could not reach GitHub: {exc.reason}. Try --local instead.")
    return {e["name"] for e in entries if e["type"] == "file" and e["name"].endswith(".md")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local",
        metavar="PATH",
        help="path to a Softorino_Support_AI checkout; skips the GitHub lookup",
    )
    args = parser.parse_args()

    referenced = routing_from_source(SOURCE)
    actual = kb_files_local(args.local) if args.local else kb_files_github()

    missing = sorted(referenced - actual)
    unroutable = sorted(actual - referenced)

    for name in missing:
        print(f"BROKEN ROUTE  {name} is routed to but does not exist in knowledge_base/")
    for name in unroutable:
        print(f"UNROUTABLE    {name} exists but no route can reach it")

    if missing or unroutable:
        print(
            f"\n{len(missing) + len(unroutable)} problem(s). "
            f"Fix the routing map in {SOURCE.relative_to(REPO_ROOT)}."
        )
        return 1

    print(f"OK -- {len(actual)} knowledge base files, all routable, no dead routes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
