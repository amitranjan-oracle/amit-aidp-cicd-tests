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


def build_signer():
    """Auto-detect an OCI signer: resource principal env, else instance principal.

    Both subclass requests.auth.AuthBase, so they plug into requests as auth=.
    Raises RuntimeError with a clear message if neither is available.
    """
    import os
    try:
        import oci
    except ImportError as exc:
        raise RuntimeError(
            "oci SDK not importable ({}); run this on the OCI box where oci is "
            "installed.".format(exc)
        )
    if os.environ.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
        log.info("Using resource-principal signer.")
        return oci.auth.signers.get_resource_principals_signer()
    try:
        log.info("Using instance-principal signer.")
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    except Exception as exc:  # not on OCI / IMDS unreachable
        raise RuntimeError(
            "No OCI principal available (no resource-principal env and "
            "instance-principal init failed: {}). Run this on the OCI box.".format(exc)
        )


class AidpClient:
    """Thin signed-HTTPS client for the AIDP data-plane."""

    def __init__(self, cfg: Dict[str, Any], signer, dry_run: bool = False) -> None:
        a = cfg["aidp"]
        self.region = a["region"]
        self.data_lake_id = a["data_lake_ocid"]
        self.path_prefix = a["path_prefix"]
        self.api_version = str(a["api_version"])
        self.workspace_key = a["workspace_key"]
        self.signer = signer
        self.verify_tls = bool(cfg.get("options", {}).get("verify_tls", True))
        self.poll_timeout = int(cfg.get("options", {}).get("poll_timeout_secs", 600))
        self.poll_interval = int(cfg.get("options", {}).get("poll_interval_secs", 5))
        self.dry_run = dry_run
        self.offline = signer is None   # no principal -> validate-only (off-box dry-run)

    # ---- URL helpers ----
    def lake_url(self, *parts: str) -> str:
        base = "https://aidp.{}.oci.oraclecloud.com/{}/{}/{}".format(
            self.region, self.api_version, self.path_prefix, self.data_lake_id)
        return "/".join([base] + [p.strip("/") for p in parts]) if parts else base

    def ws_url(self, *parts: str) -> str:
        return self.lake_url("workspaces", self.workspace_key, *parts)

    # ---- signed request ----
    def request(self, method: str, url: str, body: Optional[dict] = None,
                params: Optional[dict] = None) -> requests.Response:
        headers = {"accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")  # exact bytes the signer hashes
            headers["content-type"] = "application/json"
        log.info("%s %s", method, url)
        resp = requests.request(method, url, data=data, params=params,
                                headers=headers, auth=self.signer,
                                verify=self.verify_tls, timeout=60)
        log.info("-> HTTP %s opc-request-id=%s", resp.status_code,
                 resp.headers.get("opc-request-id"))
        return resp

    def request_ok(self, method: str, url: str, body=None, params=None,
                   ok=(200, 201, 202, 204)) -> requests.Response:
        resp = self.request(method, url, body=body, params=params)
        if resp.status_code not in ok:
            raise RuntimeError("{} {} -> HTTP {}: {} (opc-request-id={})".format(
                method, url, resp.status_code, resp.text,
                resp.headers.get("opc-request-id")))
        return resp

    # ---- Phase 1: directory ----
    def ensure_directory(self, path: str) -> None:
        if self.dry_run:
            log.info("[dry-run] mkdir %s", path); return
        resp = self.request("POST", self.ws_url("actions", "mkdir"),
                            body={"path": path, "description": None})
        if resp.status_code in (200, 201, 204):
            log.info("created directory %s", path)
        elif resp.status_code == 409 or (resp.status_code == 400
                and "exist" in (resp.text or "").lower()):
            log.info("directory %s already exists", path)
        else:
            raise RuntimeError("mkdir {} -> HTTP {}: {}".format(
                path, resp.status_code, resp.text))

    # ---- Phase 2: git folder ----
    def git_folder_metadata(self, folder_path: str) -> Dict[str, Any]:
        resp = self.request_ok("GET", self.ws_url("gitFolderMetadata"),
                               params={"folderPath": folder_path,
                                       "resourceType": "FOLDER"})
        return resp.json()

    def create_git_folder(self, folder_path, repo_url, branch, credential_key) -> None:
        if self.dry_run:
            log.info("[dry-run] create git folder %s -> %s@%s", folder_path,
                     repo_url, branch); return
        self.request_ok("POST", self.ws_url("gitFolders"), body={
            "folderPath": folder_path, "gitRepositoryUrl": repo_url,
            "branchName": branch, "credentialKey": credential_key,
            "description": None, "gitProviderKey": None})
        log.info("created git folder %s", folder_path)

    def git_pull(self, repo_key, folder_path) -> Optional[str]:
        if self.dry_run:
            log.info("[dry-run] git pull %s (repo %s)", folder_path, repo_key); return None
        resp = self.request_ok("POST",
            self.ws_url("gitRepositories", repo_key, "actions", "pull"),
            body={"gitFolderPath": folder_path, "pullAction": "PULL"})
        return (resp.headers.get("datalake-async-operation-key")
                or resp.headers.get("aidp-async-operation-key"))

    def wait_for_async(self, async_key: str) -> None:
        if not async_key:
            return
        deadline = time.time() + self.poll_timeout
        while True:
            op = self.request_ok("GET", self.lake_url("asyncOperations", async_key)).json()
            status = op.get("status")
            log.info("async %s status=%s", async_key, status)
            if status == "SUCCEEDED":
                return
            if status in ("FAILED", "CANCELED"):
                raise RuntimeError("async {} ended {}: {}".format(
                    async_key, status, op.get("messages") or op.get("message")))
            if time.time() > deadline:
                raise TimeoutError("async {} still {} after {}s".format(
                    async_key, status, self.poll_timeout))
            time.sleep(self.poll_interval)

    # ---- Phase 3: clusters ----
    def list_clusters(self) -> List[Dict[str, Any]]:
        data = self.request_ok("GET", self.ws_url("clusters")).json()
        return data.get("items", data) if isinstance(data, dict) else data

    def find_cluster_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for c in self.list_clusters():
            if c.get("displayName") == name:
                return c
        return None

    def get_cluster(self, key: str) -> Dict[str, Any]:
        return self.request_ok("GET", self.ws_url("clusters", key)).json()

    def create_cluster(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        if self.dry_run:
            log.info("[dry-run] create cluster %s", spec.get("displayName")); return {}
        return self.request_ok("POST", self.ws_url("clusters"), body=spec).json()

    def update_cluster(self, key: str, body: Dict[str, Any]) -> Any:
        if self.dry_run:
            log.info("[dry-run] update cluster %s", key); return {}
        return self.request_ok("PUT", self.ws_url("clusters", key), body=body)

    # ---- Phase 4: jobs ----
    def list_jobs(self) -> List[Dict[str, Any]]:
        data = self.request_ok("GET", self.ws_url("jobs")).json()
        return data.get("items", data) if isinstance(data, dict) else data

    def find_job_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for j in self.list_jobs():
            if j.get("name") == name:
                return j
        return None

    def get_job(self, key: str) -> Dict[str, Any]:
        return self.request_ok("GET", self.ws_url("jobs", key)).json()

    def create_job(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        if self.dry_run:
            log.info("[dry-run] create job %s", spec.get("name")); return {}
        return self.request_ok("POST", self.ws_url("jobs"), body=spec).json()

    def update_job(self, key: str, body: Dict[str, Any]) -> Any:
        if self.dry_run:
            log.info("[dry-run] update job %s", key); return {}
        return self.request_ok("PUT", self.ws_url("jobs", key), body=body)
