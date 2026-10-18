#!/usr/bin/env python3
"""charts.py — carry Helm charts across the air gap as OCI artifacts.

Book: Sovereign AI Stacks, Chapter 3
Requires: Python 3.13, PyYAML 6.0, helm 3.22

zarf.yaml is the single source of truth for which charts (and which versions)
the platform uses. This script reads it so the chart list is never maintained
twice.

    # connected side: download every chart in zarf.yaml
    python charts.py pull --zarf ../zarf.yaml --out /transfer/charts

    # enclave side: push them into Harbor's OCI chart project
    python charts.py push --dir /import/charts --registry oci://harbor.enclave.internal/charts
"""

from __future__ import annotations

import argparse
import shutil
import subprocess  # noqa: S404 — we run helm with argument lists, never a shell
import sys
from pathlib import Path

import yaml


def charts_from_zarf(zarf_file: Path) -> list[dict]:
    """Return de-duplicated chart entries from every component in zarf.yaml."""
    doc = yaml.safe_load(zarf_file.read_text(encoding="utf-8"))
    seen: set[tuple[str, str]] = set()
    charts: list[dict] = []
    for component in doc.get("components", []):
        for chart in component.get("charts", []):
            if "url" not in chart:
                continue  # local charts live in the repo already
            # Zarf uses repoName when the chart's name in the repo differs
            # from the release name.
            name = chart.get("repoName", chart["name"])
            key = (chart["url"], name)
            if key in seen:
                continue
            seen.add(key)
            charts.append({"name": name, "url": chart["url"], "version": str(chart["version"])})
    return charts


def helm(*args: str) -> None:
    helm_bin = shutil.which("helm")
    if helm_bin is None:
        raise SystemExit("helm not found on PATH")
    subprocess.run([helm_bin, *args], check=True)  # noqa: S603


def pull(zarf_file: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for c in charts_from_zarf(zarf_file):
        if c["url"].startswith("oci://"):
            helm("pull", c["url"], "--version", c["version"], "--destination", str(out))
        else:
            helm("pull", c["name"], "--repo", c["url"], "--version", c["version"], "--destination", str(out))
        print(f"pulled {c['name']} {c['version']}")
    return 0


def push(chart_dir: Path, registry: str) -> int:
    if not registry.startswith("oci://"):
        raise SystemExit("--registry must start with oci://")
    packages = sorted(chart_dir.glob("*.tgz"))
    if not packages:
        raise SystemExit(f"no .tgz charts in {chart_dir}")
    for pkg in packages:
        helm("push", str(pkg), registry)
    print(f"pushed {len(packages)} charts to {registry}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("pull")
    pp.add_argument("--zarf", type=Path, required=True)
    pp.add_argument("--out", type=Path, required=True)
    ps = sub.add_parser("push")
    ps.add_argument("--dir", type=Path, required=True)
    ps.add_argument("--registry", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--zarf", type=Path, required=True)
    args = p.parse_args(argv)

    if args.cmd == "pull":
        return pull(args.zarf, args.out)
    if args.cmd == "push":
        return push(args.dir, args.registry)
    for c in charts_from_zarf(args.zarf):
        print(f"{c['name']:<28} {c['version']:<12} {c['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
