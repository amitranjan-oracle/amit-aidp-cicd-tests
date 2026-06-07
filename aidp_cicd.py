#!/usr/bin/env python3
"""AIDP CI/CD reconcile script.

Runs on amit-cicd-compute (Python 3.9) under the box's instance principal.
Reads config/cicd.yaml + specs/*.json and brings AIDP to desired state:
  Phase 1  ensure /Workspace/cicd_folder
  Phase 2  git folder clone (create) or pull main
  Phase 3  reconcile compute cluster ephemeral_01
  Phase 4  reconcile workflow job cicd_workflow_job (cluster key injected)
Only oci + requests + yaml + stdlib. Python 3.9 compatible.
"""

import argparse
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

import requests
import yaml

log = logging.getLogger("aidp-cicd")

# Required config keys as (section, key) pairs.
REQUIRED_CONFIG_KEYS = [
    ("aidp", "region"), ("aidp", "data_lake_ocid"), ("aidp", "path_prefix"),
    ("aidp", "api_version"), ("aidp", "workspace_key"),
    ("git", "repository_url"), ("git", "branch"), ("git", "credential_key"),
    ("git", "parent_dir"), ("git", "folder_path"),
    ("compute", "name"), ("compute", "spec_file"),
    ("workflow", "name"), ("workflow", "spec_file"), ("workflow", "cluster_name"),
]


def load_config(path: str) -> Dict[str, Any]:
    """Load + validate the YAML config; raise ValueError listing all missing keys."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    missing = []
    for section, key in REQUIRED_CONFIG_KEYS:
        sec = cfg.get(section)
        val = sec.get(key) if isinstance(sec, dict) else None
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append("{}.{}".format(section, key))
    if missing:
        raise ValueError("Missing required config keys: " + ", ".join(missing))
    return cfg


def load_spec(path: str) -> Dict[str, Any]:
    """Load a JSON desired-state spec file."""
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON in spec {}: {}".format(path, exc)) from exc


def satisfies(desired: Any, live: Any) -> bool:
    """True if every value declared in `desired` is present/equal in `live`.

    Extra keys in `live` are ignored (declarative subset semantics), so
    server-defaulted fields never trigger a false 'differs'.
    """
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return False
        return all(k in live and satisfies(v, live[k]) for k, v in desired.items())
    if isinstance(desired, list):
        if not isinstance(live, list) or len(desired) != len(live):
            return False
        return all(satisfies(dv, lv) for dv, lv in zip(desired, live))
    return desired == live


def _strip_cluster_keys(job: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-copy a job and drop volatile clusterKey from all cluster refs."""
    j = json.loads(json.dumps(job))
    for jc in (j.get("jobClusters") or []):
        jc.pop("clusterKey", None)
    for t in (j.get("tasks") or []):
        c = t.get("cluster")
        if isinstance(c, dict):
            c.pop("clusterKey", None)
    return j


def _sort_job_lists(job: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-copy a job and sort its tasks/jobClusters so comparison is order-independent."""
    j = json.loads(json.dumps(job))
    if isinstance(j.get("tasks"), list):
        j["tasks"] = sorted(j["tasks"], key=lambda t: t.get("taskKey") or "")
    if isinstance(j.get("jobClusters"), list):
        j["jobClusters"] = sorted(j["jobClusters"], key=lambda c: c.get("clusterName") or "")
    return j


def cluster_in_sync(desired: Dict[str, Any], live: Dict[str, Any]) -> bool:
    """Cluster reconcile check — pure subset (volatile fields aren't in desired)."""
    return satisfies(desired, live)


def job_in_sync(desired: Dict[str, Any], live: Dict[str, Any]) -> bool:
    """Job reconcile check — clusterKey stripped (match by name), order-independent."""
    d = _sort_job_lists(_strip_cluster_keys(desired))
    l = _sort_job_lists(_strip_cluster_keys(live))
    return satisfies(d, l)


def inject_cluster_key(job_spec: Dict[str, Any], cluster_key: str) -> Dict[str, Any]:
    """Return a deep copy of the job spec with clusterKey set on every cluster ref."""
    j = json.loads(json.dumps(job_spec))
    for jc in (j.get("jobClusters") or []):
        jc["clusterKey"] = cluster_key
    for t in (j.get("tasks") or []):
        c = t.get("cluster")
        if isinstance(c, dict):
            c["clusterKey"] = cluster_key
    return j
