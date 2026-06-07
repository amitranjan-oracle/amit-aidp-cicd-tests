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
