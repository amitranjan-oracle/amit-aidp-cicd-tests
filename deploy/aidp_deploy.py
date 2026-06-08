#!/usr/bin/env python3
"""AIDP CI/CD reconcile script.

Runs on amit-cicd-compute (Python 3.9) under the box's instance principal.
Reads deploy/cicd.yaml + specs/*.json and brings AIDP to desired state:
  Phase 0  ensure the GIT_ACCOUNT user-setting (create from OCI secret if absent)
  Phase 1  ensure /Workspace/cicd_folder
  Phase 2  git folder clone (create) or pull main
  Phase 3  reconcile compute cluster cicd_01
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

# API version is paired with the path-prefix surface; an explicit aidp.api_version
# overrides this (mirrors ai-data-engineer-agent's base_client default_api_version_for).
_PREFIX_API_VERSION = {"aiDataPlatforms": "20260430", "dataLakes": "20240831"}
DEFAULT_API_VERSION = "20260430"

# Per-runner profile. --runner selects BOTH the AIDP signer and the git folder.
# Both VM and OKE authenticate to AIDP with the host's *instance* principal:
# OKE Workload Identity authenticates fine but AIDP does NOT authorize WI
# principals for workspace volume/list ops (see docs/aidp-wi-rbac-issue.md), so
# the OKE node's instance principal is used — same identity model as the VM.
# (Change RUNNER_AUTH["oke"] to "oke_workload_identity" if AIDP ever fixes that.)
RUNNER_AUTH = {"vm": "instance_principal", "oke": "instance_principal"}
# Distinct folder per runner: per-instance-principal credential ownership means
# the VM and OKE node can't share one git folder.
RUNNER_FOLDER_SUFFIX = {"vm": "", "oke": "-oke"}


def default_api_version_for(path_prefix: str) -> str:
    """API version paired with *path_prefix* (dataLakes->20240831, aiDataPlatforms->20260430)."""
    return _PREFIX_API_VERSION.get(path_prefix, DEFAULT_API_VERSION)


# Required config keys. Other values are DERIVED, not configured:
#   aidp.api_version      <- default_api_version_for(path_prefix) [override optional]
#   git.folder_path       <- parent_dir + repo name (from repository_url) [AIDP_FOLDER_PATH override]
#   compute.name          <- compute spec's displayName
#   workflow.name         <- workflow spec's name
#   workflow.cluster_name <- workflow spec's jobClusters[].clusterName
REQUIRED_CONFIG_KEYS = [
    ("aidp", "region"), ("aidp", "data_lake_ocid"), ("aidp", "path_prefix"),
    ("aidp", "workspace_key"),
    ("git", "repository_url"), ("git", "branch"),
    ("git", "credential_name"), ("git", "credential_secret_id"),
    ("git", "credential_username"), ("git", "parent_dir"),
    ("compute", "spec_file"),
    ("workflow", "spec_file"),
]


def resolve_folder_path(cfg: Dict[str, Any], runner: str = "vm") -> str:
    """The AIDP git folder path: env AIDP_FOLDER_PATH wins (explicit override),
    else git.parent_dir + the repo name parsed from git.repository_url, with a
    per-runner suffix so independent runners own distinct folders.

    Per-instance-principal credential ownership means the VM and OKE node can't
    share one folder (each pull credential is owned by one principal), so the
    OKE runner gets a '-oke' suffix (e.g. .../amit-aidp-cicd-tests-oke)."""
    import os
    env = os.environ.get("AIDP_FOLDER_PATH")
    if env:
        return env
    g = cfg["git"]
    repo = g["repository_url"].rstrip("/").split("/")[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    base = g["parent_dir"].rstrip("/") + "/" + repo
    return base + RUNNER_FOLDER_SUFFIX.get(runner, "")


def load_config(path: str) -> Dict[str, Any]:
    """Load + validate the YAML config; raise ValueError listing all missing keys.

    git.parent_dir may be overridden per-runner via AIDP_PARENT_DIR (and the
    derived folder path via AIDP_FOLDER_PATH) so independent runners target
    distinct AIDP git folders — per-instance-principal credential ownership means
    two runners can't share one folder's pull credential.
    """
    import os
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    v = os.environ.get("AIDP_PARENT_DIR")
    if v and isinstance(cfg.get("git"), dict):
        cfg["git"]["parent_dir"] = v
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
    d.pop("path", None)  # server normalizes path inconsistently (create vs update) — not a real diff
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


def select_auth_method(env: Dict[str, str]) -> str:
    """Pick the OCI signer to build, by priority:
    OKE workload identity (explicit) > resource principal (env) > instance principal.
    Pure function over an environment mapping so it is unit-testable.
    """
    if env.get("AIDP_AUTH_METHOD", "").strip().lower() == "oke_workload_identity":
        return "oke_workload_identity"
    if env.get("OCI_RESOURCE_PRINCIPAL_VERSION"):
        return "resource_principal"
    return "instance_principal"


def build_signer(method: Optional[str] = None):
    """Build an OCI signer for *method* (an explicit auth method, e.g. selected by
    --runner via RUNNER_AUTH); when None, fall back to select_auth_method(env).
    All signers subclass requests.auth.AuthBase so they plug into requests as auth=.
    Raises RuntimeError with a clear message if none is available.
    """
    import os
    try:
        import oci
    except ImportError as exc:
        raise RuntimeError(
            "oci SDK not importable ({}); run this where oci is installed.".format(exc))
    if method is None:
        method = select_auth_method(os.environ)
    if method == "oke_workload_identity":
        log.info("Using OKE workload-identity signer.")
        return oci.auth.signers.get_oke_workload_identity_resource_principal_signer()
    if method == "resource_principal":
        log.info("Using resource-principal signer.")
        return oci.auth.signers.get_resource_principals_signer()
    try:
        log.info("Using instance-principal signer.")
        return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    except Exception as exc:  # not on OCI / IMDS unreachable
        raise RuntimeError(
            "No OCI principal available (instance-principal init failed: {}). "
            "Run this on the OCI box / OKE pod.".format(exc))


def _async_key(resp) -> Optional[str]:
    """Extract an AIDP async-operation key from a response's headers, if present."""
    return (resp.headers.get("datalake-async-operation-key")
            or resp.headers.get("aidp-async-operation-key"))


def _find_setting_key_by_name(settings: List[Dict[str, Any]], name: str) -> Optional[str]:
    """Return the `key` of the userSetting whose `name` matches, else None."""
    for s in settings:
        if s.get("name") == name:
            return s.get("key")
    return None


def _parse_iso(ts: Optional[str]):
    """Parse an ISO-8601 timestamp (e.g. '2026-05-08T22:11:19.646Z') to an aware
    datetime, tolerating a trailing 'Z' and variable fractional digits. Returns
    None if absent/unparseable (callers treat None as 'unknown -> recreate')."""
    if not ts:
        return None
    import re
    from datetime import datetime
    s = ts.strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2})?$", s)
    if not m:
        return None
    base, frac, off = m.group(1), m.group(2) or "", m.group(3) or "+00:00"
    if frac:
        frac = (frac + "000000")[:7]  # normalize to 6 fractional digits for py3.9
    try:
        return datetime.fromisoformat(base + frac + off)
    except ValueError:
        return None


def _ws_relpath(path: str) -> str:
    """Workspace-root-relative path for the gitFolders API ("must be relative").

    `/Workspace/cicd_folder/aidp-tests` -> `cicd_folder/aidp-tests`.
    (mkdir / gitFolderMetadata accept the absolute form; gitFolders create/pull do not.)
    """
    p = path.lstrip("/")
    if p.startswith("Workspace/"):
        p = p[len("Workspace/"):]
    return p


class AidpClient:
    """Thin signed-HTTPS client for the AIDP data-plane."""

    def __init__(self, cfg: Dict[str, Any], signer, dry_run: bool = False) -> None:
        a = cfg["aidp"]
        self.region = a["region"]
        self.data_lake_id = a["data_lake_ocid"]
        self.path_prefix = a["path_prefix"]
        self.api_version = str(a.get("api_version") or default_api_version_for(self.path_prefix))
        self.workspace_key = a["workspace_key"]
        self.signer = signer
        self.verify_tls = bool(cfg.get("options", {}).get("verify_tls", True))
        self.poll_timeout = int(cfg.get("options", {}).get("poll_timeout_secs", 600))
        self.poll_interval = int(cfg.get("options", {}).get("poll_interval_secs", 5))
        self.dry_run = dry_run
        self.offline = signer is None   # no principal -> validate-only (off-box dry-run)
        self.runner = "vm"              # set by run() from --runner; drives the folder suffix

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

    def list_all(self, url: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
        """GET every item across ALL pages, following the `opc-next-page` header.

        AIDP list endpoints paginate (default page ~100 items). A single GET only
        returns the first page, so a name lookup over one page MISSES any resource
        past page 1 — which then surfaces as a spurious CREATE -> HTTP 409
        "already exists". Always paginate list endpoints used for find-by-name."""
        params = dict(params or {})
        items: List[Dict[str, Any]] = []
        while True:
            resp = self.request_ok("GET", url, params=params)
            data = resp.json()
            page = data.get("items", data) if isinstance(data, dict) else data
            if not isinstance(page, list):
                return page  # unexpected shape; hand back as-is
            items.extend(page)
            next_page = resp.headers.get("opc-next-page")
            if not next_page or not page:
                return items
            params["page"] = next_page

    # ---- credentials (GIT_ACCOUNT userSettings, resolved in caller identity) ----
    def list_git_account_settings(self) -> List[Dict[str, Any]]:
        return self.list_all(self.lake_url("userSettings"),
                             params={"settingType": "GIT_ACCOUNT"})

    def _find_setting_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full GIT_ACCOUNT userSetting dict matching name, else None."""
        for s in self.list_git_account_settings():
            if s.get("name") == name:
                return s
        return None

    def _read_secret(self, secret_id: str, compartment: Optional[str]):
        """Read (pat, version_time) from an OCI Vault secret (by OCID, or by name
        within *compartment*). Requires the running principal to have `read
        secret-bundles` on the secret. version_time is the current version's
        creation time (an aware datetime), used to detect PAT rotation."""
        import base64
        import oci
        if not secret_id.startswith("ocid1.vaultsecret"):
            if not compartment:
                raise RuntimeError(
                    "git.credential_secret_id {!r} is a name; set "
                    "git.credential_secret_compartment to resolve it.".format(secret_id))
            vc = oci.vault.VaultsClient({"region": self.region}, signer=self.signer)
            items = vc.list_secrets(compartment_id=compartment, name=secret_id).data
            if not items:
                raise RuntimeError("no OCI secret named {!r} in compartment {}".format(
                    secret_id, compartment))
            secret_id = items[0].id
        bundle = oci.secrets.SecretsClient(
            {"region": self.region}, signer=self.signer).get_secret_bundle(secret_id).data
        pat = base64.b64decode(bundle.secret_bundle_content.content).decode("utf-8").strip()
        return pat, getattr(bundle, "time_created", None)

    def delete_user_setting(self, key: str) -> None:
        self.request_ok("DELETE", self.lake_url("userSettings", key), ok=(200, 202, 204))

    def ensure_git_credential(self, cfg: Dict[str, Any]) -> str:
        """Reconcile the GIT_ACCOUNT user-setting named git.credential_name against
        the OCI secret (source of truth), under the current principal:
          absent                       -> create from secret
          present + secret rotated     -> delete + recreate (new PAT picked up)
          present + not rotated         -> no-op
          present + secret unreadable  -> keep existing (best-effort; warn)
        Created under whoever runs this (VM instance principal / OKE workload
        identity), so the setting is always owned by — and visible to — that
        principal. Returns the resolved key."""
        g = cfg["git"]
        name = g["credential_name"]
        existing = self._find_setting_by_name(name)
        try:
            pat, secret_time = self._read_secret(
                g["credential_secret_id"], g.get("credential_secret_compartment"))
        except Exception as exc:  # noqa: BLE001 - fall back to an existing credential
            if existing:
                log.warning("cannot read secret %s (%s); keeping existing credential %r",
                            g["credential_secret_id"], exc, name)
                return existing["key"]
            raise RuntimeError("git credential {!r} is absent and its secret is "
                               "unreadable: {}".format(name, exc))
        if existing:
            updated = _parse_iso(existing.get("timeUpdated"))
            if secret_time is not None and updated is not None and secret_time <= updated:
                log.info("git credential %r is up to date (secret not rotated since)", name)
                return existing["key"]
            log.info("git credential %r stale/rotated; recreating from secret", name)
            self.delete_user_setting(existing["key"])
        body = {"name": name, "isDefault": False,
                "data": {"type": "GIT_ACCOUNT", "providerName": "GITHUB",
                         "entityType": "PERSONAL_ACCESS_TOKEN",
                         "username": g["credential_username"],
                         "personalAccessToken": pat}}
        key = self.request_ok("POST", self.lake_url("userSettings"), body=body).json().get("key")
        log.info("git credential %r created (key %s)", name, key)
        return key

    def resolve_git_credential_key(self, cfg: Dict[str, Any]) -> str:
        """Resolve the key of the GIT_ACCOUNT setting named git.credential_name
        under the current principal (ensure_git_credential guarantees it exists)."""
        name = cfg["git"]["credential_name"]
        key = _find_setting_key_by_name(self.list_git_account_settings(), name)
        if not key:
            raise RuntimeError(
                "GIT_ACCOUNT credential named {!r} not found under the current "
                "principal — phase 0 should have created it.".format(name))
        log.info("resolved git credential %r -> key %s", name, key)
        return key

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
        # Must use the workspace-relative path — the absolute form never matches.
        resp = self.request_ok("GET", self.ws_url("gitFolderMetadata"),
                               params={"folderPath": _ws_relpath(folder_path),
                                       "resourceType": "FOLDER"})
        return resp.json()

    def create_git_folder(self, folder_path, repo_url, branch, credential_key) -> Optional[str]:
        if self.dry_run:
            log.info("[dry-run] create git folder %s -> %s@%s", folder_path,
                     repo_url, branch); return None
        resp = self.request_ok("POST", self.ws_url("gitFolders"), body={
            "folderPath": _ws_relpath(folder_path), "gitRepositoryUrl": repo_url,
            "branchName": branch, "credentialKey": credential_key,
            "description": None, "gitProviderKey": None})
        log.info("created git folder %s (cloning async)", folder_path)
        return _async_key(resp)

    def git_pull(self, repo_key, folder_path, branch) -> Optional[str]:
        if self.dry_run:
            log.info("[dry-run] git pull %s (repo %s, branch %s)", folder_path,
                     repo_key, branch); return None
        resp = self.request_ok("POST",
            self.ws_url("gitRepositories", repo_key, "actions", "pull"),
            body={"branchName": branch, "gitFolderPath": _ws_relpath(folder_path),
                  "pullAction": "PULL"})
        return _async_key(resp)

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

    def _wait_if_async(self, resp) -> None:
        """If a mutating response carries an async-operation key, poll it to completion."""
        key = _async_key(resp)
        if key:
            self.wait_for_async(key)

    # ---- Phase 3: clusters ----
    def list_clusters(self) -> List[Dict[str, Any]]:
        return self.list_all(self.ws_url("clusters"))

    def find_cluster_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for c in self.list_clusters():
            if c.get("displayName") == name:
                return c
        return None

    def get_cluster(self, key: str) -> Dict[str, Any]:
        return self.request_ok("GET", self.ws_url("clusters", key)).json()

    def wait_for_cluster_active(self, key: str) -> Dict[str, Any]:
        """Poll the cluster until it reports ACTIVE (create/restart provisioning
        completes). Raise on FAILED, or TimeoutError past poll_timeout."""
        deadline = time.time() + self.poll_timeout
        while True:
            c = self.get_cluster(key)
            state = c.get("state") or c.get("lifecycleState")
            log.info("cluster %s state=%s", key, state)
            if state == "ACTIVE":
                return c
            if state == "FAILED":
                raise RuntimeError("cluster {} entered FAILED: {}".format(
                    key, c.get("stateDetails")))
            if time.time() > deadline:
                raise TimeoutError("cluster {} still {} after {}s".format(
                    key, state, self.poll_timeout))
            time.sleep(self.poll_interval)

    def create_cluster(self, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self.dry_run:
            log.info("[dry-run] create cluster %s", spec.get("displayName")); return None
        resp = self.request_ok("POST", self.ws_url("clusters"), body=spec)
        self._wait_if_async(resp)
        try:
            created = resp.json()
        except ValueError:
            created = {}
        key = created.get("key")
        if not key:  # response didn't echo the key — resolve by name
            found = self.find_cluster_by_name(spec.get("displayName"))
            key = found.get("key") if found else None
        log.info("created cluster %s (key=%s); waiting for ACTIVE", spec.get("displayName"), key)
        if key:
            self.wait_for_cluster_active(key)
        return created

    def update_cluster(self, key: str, body: Dict[str, Any]) -> None:
        if self.dry_run:
            log.info("[dry-run] update cluster %s", key); return
        self._wait_if_async(self.request_ok("PUT", self.ws_url("clusters", key), body=body))
        log.info("updated cluster %s; waiting for ACTIVE", key)
        self.wait_for_cluster_active(key)

    # ---- Phase 4: jobs ----
    def list_jobs(self) -> List[Dict[str, Any]]:
        return self.list_all(self.ws_url("jobs"))

    def find_job_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for j in self.list_jobs():
            if j.get("name") == name:
                return j
        return None

    def get_job(self, key: str) -> Dict[str, Any]:
        return self.request_ok("GET", self.ws_url("jobs", key)).json()

    def create_job(self, spec: Dict[str, Any]) -> None:
        if self.dry_run:
            log.info("[dry-run] create job %s", spec.get("name")); return
        resp = self.request_ok("POST", self.ws_url("jobs"), body=spec)
        self._wait_if_async(resp)
        log.info("created job %s", spec.get("name"))

    def update_job(self, key: str, body: Dict[str, Any]) -> None:
        if self.dry_run:
            log.info("[dry-run] update job %s", key); return
        self._wait_if_async(self.request_ok("PUT", self.ws_url("jobs", key), body=body))
        log.info("updated job %s", key)


def phase0_credential(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 0: ensure git credential ==")
    if client.offline:
        log.info("[offline dry-run] would ensure git credential %r (from secret %s)",
                 cfg["git"]["credential_name"], cfg["git"]["credential_secret_id"]); return
    client.ensure_git_credential(cfg)


def phase1_directory(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 1: ensure directory ==")
    client.ensure_directory(cfg["git"]["parent_dir"])


def phase2_git_folder(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 2: git folder (create or pull) ==")
    g = cfg["git"]
    fp = resolve_folder_path(cfg, client.runner)  # parent_dir + repo (+ runner suffix), or AIDP_FOLDER_PATH
    if client.offline:
        log.info("[offline dry-run] would create-or-pull git folder %s (%s@%s)",
                 fp, g["repository_url"], g["branch"]); return
    meta = client.git_folder_metadata(fp)
    if meta.get("isAssociated") and meta.get("repoKey"):
        log.info("git folder exists; pulling %s", g["branch"])
        client.wait_for_async(client.git_pull(meta["repoKey"], fp, g["branch"]))
    else:
        log.info("git folder absent; cloning")
        cred_key = client.resolve_git_credential_key(cfg)
        client.wait_for_async(client.create_git_folder(
            fp, g["repository_url"], g["branch"], cred_key))


def phase3_compute(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 3: reconcile compute ==")
    desired = load_spec(cfg["compute"]["spec_file"])
    name = desired.get("displayName")  # derived from the spec (not duplicated in config)
    if client.offline:
        log.info("[offline dry-run] validated spec for cluster %s; skipping live check", name); return
    found = client.find_cluster_by_name(name)
    if found is None:
        log.info("cluster %s absent -> CREATE", name)
        client.create_cluster(desired)
        return
    live = client.get_cluster(found["key"])  # full repr — the list view summarizes config
    if cluster_in_sync(desired, live):
        log.info("cluster %s already in sync -> NO-OP", name)
    else:
        log.info("cluster %s differs -> UPDATE", name)
        client.update_cluster(found["key"], {**live, **desired})


def phase4_job(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 4: reconcile job ==")
    desired = load_spec(cfg["workflow"]["spec_file"])
    name = desired.get("name")  # derived from the spec
    jobclusters = desired.get("jobClusters") or []
    cluster_name = jobclusters[0].get("clusterName") if jobclusters else None  # cluster the job binds to
    if client.offline:
        log.info("[offline dry-run] validated spec for job %s; skipping live check", name); return
    if not cluster_name:
        raise RuntimeError("job spec {} has no jobClusters[].clusterName to bind".format(
            cfg["workflow"]["spec_file"]))
    cluster = client.find_cluster_by_name(cluster_name)
    if cluster is None:
        raise RuntimeError("cluster {} not found; Phase 3 must create it first".format(cluster_name))
    desired_keyed = inject_cluster_key(desired, cluster["key"])
    found = client.find_job_by_name(name)
    if found is None:
        log.info("job %s absent -> CREATE", name)
        client.create_job(desired_keyed)
        return
    current = client.get_job(found["key"])  # full repr for an accurate diff
    if job_in_sync(desired, current):
        log.info("job %s already in sync -> NO-OP", name)
    else:
        log.info("job %s differs -> UPDATE", name)
        client.update_job(found["key"], {**current, **desired_keyed})


def _build_signer_with_timeout(timeout_secs: float, method: Optional[str] = None):
    """Call build_signer(method) in a daemon thread; return (signer, None) on
    success or (None, exc_str) if it raises or times out within *timeout_secs*."""
    import threading
    result: list = []

    def _target():
        try:
            result.append(("ok", build_signer(method)))
        except Exception as exc:  # noqa: BLE001
            result.append(("err", str(exc)))

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_secs)
    if not result:
        return None, "build_signer timed out after {}s (IMDS unreachable)".format(timeout_secs)
    tag, val = result[0]
    if tag == "ok":
        return val, None
    return None, val


def run(cfg: Dict[str, Any], dry_run: bool, runner: str = "vm") -> None:
    # The runner profile selects the AIDP signer (RUNNER_AUTH) and git folder.
    method = RUNNER_AUTH.get(runner, "instance_principal")
    log.info("Runner profile: %s (auth=%s)", runner, method)
    # Always try for a signer. On-box (incl. dry-run) we get one and make real
    # read-only decisions. Off-box dry-run tolerates no principal -> offline.
    signer = None
    try:
        if dry_run:
            # Cap the IMDS probe at 5 s so off-box dry-runs don't hang.
            signer, err = _build_signer_with_timeout(5, method)
            if signer is None:
                raise RuntimeError(err)
        else:
            signer = build_signer(method)
    except RuntimeError as exc:
        if not dry_run:
            raise
        log.warning("No OCI principal (%s); offline dry-run: config/specs only.", exc)
    client = AidpClient(cfg, signer, dry_run=dry_run)
    client.runner = runner
    phase0_credential(client, cfg)
    phase1_directory(client, cfg)
    phase2_git_folder(client, cfg)
    phase3_compute(client, cfg)
    phase4_job(client, cfg)
    log.info("== Done ==")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AIDP CI/CD reconcile")
    parser.add_argument("--config", required=True)
    parser.add_argument("--runner", choices=sorted(RUNNER_AUTH), default="vm",
                        help="Which runner this executes on (vm|oke); selects the "
                             "AIDP signer (RUNNER_AUTH) and the git folder suffix.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + log intended actions; mutate nothing.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.dry_run:
        log.info("DRY RUN — no mutations will be made.")
    run(cfg, dry_run=args.dry_run, runner=args.runner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
