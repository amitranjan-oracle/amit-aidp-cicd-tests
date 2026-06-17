#!/usr/bin/env python3
"""AIDP CI/CD bundle-deploy script.

Runs on the self-hosted runner under the box's instance principal. Reads
deploy/cicd.yaml and:
  Phase 0  ensure the GIT_ACCOUNT user-setting (create from the OCI secret if absent)
  Phase 1  ensure the workspace git.parent_dir exists
  Phase 2  clone the git repo into it (or pull if present); ensure the folder is
           bound to the running principal's credential (re-associate if not)
  Phase 3  deploy the AIDP bundle at git.bundle_path — which creates/updates the
           bundle's own compute + workflow
  Phase 4  ensure each deployed job's runAs matches its bundle job json. AIDP's
           job CREATE drops runAs (only UPDATE persists it), so a *fresh* bundle
           deploy leaves runAs=null and a scheduled job then fails; re-apply it
           here via UpdateJob.
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

# --runner selects ONLY the AIDP signer; VM and OKE are otherwise identical
# (same config, same git folder, same bundle). Both authenticate with the host's
# *instance* principal: OKE Workload Identity authenticates but AIDP does NOT
# authorize WI principals for workspace volume/list ops (docs/aidp-wi-rbac-issue.md),
# so the OKE node's instance principal is used. A shared folder is safe because
# Phase 2 re-binds it to the running principal's credential each run; CICD also
# serializes VM/OKE via a shared concurrency group so deploys never race.
# (Change RUNNER_AUTH["oke"] to "oke_workload_identity" if AIDP ever fixes that.)
RUNNER_AUTH = {"vm": "instance_principal", "oke": "instance_principal"}


def default_api_version_for(path_prefix: str) -> str:
    """API version paired with *path_prefix* (dataLakes->20240831, aiDataPlatforms->20260430)."""
    return _PREFIX_API_VERSION.get(path_prefix, DEFAULT_API_VERSION)


# Required config keys. Other values are DERIVED, not configured:
#   aidp.api_version   <- default_api_version_for(path_prefix) [override optional]
#   git.folder_path    <- parent_dir + repo name (from repository_url) [AIDP_FOLDER_PATH override]
#   bundle deploy path <- git folder path + "/" + git.bundle_path
REQUIRED_CONFIG_KEYS = [
    ("aidp", "region"), ("aidp", "data_lake_ocid"), ("aidp", "path_prefix"),
    ("aidp", "workspace_key"),
    ("git", "repository_url"), ("git", "branch"),
    ("git", "credential_name"), ("git", "credential_secret_id"),
    ("git", "credential_username"), ("git", "parent_dir"),
    ("git", "bundle_path"),
]


def resolve_folder_path(cfg: Dict[str, Any]) -> str:
    """The AIDP git folder path: env AIDP_FOLDER_PATH wins (explicit override),
    else git.parent_dir + the repo name parsed from git.repository_url.

    VM and OKE share one folder — Phase 2 re-binds it to the running principal's
    credential, so per-principal credential ownership no longer forces separate
    folders per runner."""
    import os
    env = os.environ.get("AIDP_FOLDER_PATH")
    if env:
        return env
    g = cfg["git"]
    repo = g["repository_url"].rstrip("/").split("/")[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return g["parent_dir"].rstrip("/") + "/" + repo


def load_config(path: str) -> Dict[str, Any]:
    """Load + validate the YAML config; raise ValueError listing all missing keys.

    git.parent_dir may be overridden via AIDP_PARENT_DIR (and the derived folder
    path via AIDP_FOLDER_PATH) as an operational escape hatch. VM and OKE share
    one folder — Phase 2 re-binds it to the running principal's credential.
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
        # Bundle endpoints are served on the aiDataPlatforms surface (20260430),
        # NOT dataLakes/20240831 (which 404s "Unknown resource: <dataLakeId>") —
        # same data-lake id, different prefix+version. git/credential/dir ops stay
        # on the configured (dataLakes) surface, which is proven for them.
        self.bundle_path_prefix = a.get("bundle_path_prefix", "aiDataPlatforms")
        self.bundle_api_version = str(a.get("bundle_api_version")
                                      or default_api_version_for(self.bundle_path_prefix))
        self.workspace_key = a["workspace_key"]
        self.signer = signer
        self.verify_tls = bool(cfg.get("options", {}).get("verify_tls", True))
        self.poll_timeout = int(cfg.get("options", {}).get("poll_timeout_secs", 600))
        self.poll_interval = int(cfg.get("options", {}).get("poll_interval_secs", 5))
        # Phase 4 re-applies runAs via UpdateJob (CREATE drops it). Setting runAs
        # needs ADMIN on the job; the creator normally has it immediately, but we
        # briefly retry a transient 404 NotAuthorizedOrNotFound ("Permission Type:
        # ADMIN") in case the per-job admin grant ever propagates asynchronously.
        # NOTE: a 404 here can ALSO mean runAs is not a valid credential KEY (a
        # display name is rejected the same way) — that will NOT clear with retries.
        self.runas_grant_timeout = int(
            cfg.get("options", {}).get("runas_grant_timeout_secs", 120))
        self.dry_run = dry_run
        self.offline = signer is None   # no principal -> validate-only (off-box dry-run)

    # ---- URL helpers ----
    def _surface_url(self, api_version: str, path_prefix: str, *parts: str) -> str:
        base = "https://aidp.{}.oci.oraclecloud.com/{}/{}/{}".format(
            self.region, api_version, path_prefix, self.data_lake_id)
        return "/".join([base] + [p.strip("/") for p in parts]) if parts else base

    def lake_url(self, *parts: str) -> str:
        return self._surface_url(self.api_version, self.path_prefix, *parts)

    def ws_url(self, *parts: str) -> str:
        return self.lake_url("workspaces", self.workspace_key, *parts)

    def bundle_ws_url(self, *parts: str) -> str:
        # Bundle ops live on the aiDataPlatforms surface (see __init__).
        return self._surface_url(self.bundle_api_version, self.bundle_path_prefix,
                                 "workspaces", self.workspace_key, *parts)

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
                raise RuntimeError("GET {} returned a non-list page ({}): {!r}".format(
                    url, type(page).__name__, data))
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
        # Poll if the delete is async (202) so a same-name recreate can't race it.
        self._wait_if_async(self.request_ok(
            "DELETE", self.lake_url("userSettings", key), ok=(200, 202, 204)))

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
        if self.dry_run:  # dry-run must never delete/recreate the credential
            existing = self._find_setting_by_name(name)
            log.info("[dry-run] would reconcile git credential %r from secret %s (currently %s)",
                     name, g["credential_secret_id"], "present" if existing else "absent")
            return existing["key"] if existing else None
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
            if self.dry_run:
                # In dry-run, phase 0 doesn't actually create the credential, so a
                # first-run absence is expected — don't fail; downstream clone/pull
                # are dry-run no-ops anyway.
                log.info("[dry-run] git credential %r not present yet (phase 0 would "
                         "create it)", name)
                return None
            raise RuntimeError(
                "GIT_ACCOUNT credential named {!r} not found under the current "
                "principal — phase 0 should have created it.".format(name))
        log.info("resolved git credential %r -> key %s", name, key)
        return key

    # ---- Phase 1: directory ----
    def ensure_directory(self, path: str) -> bool:
        """Create the directory. Return True if it was newly created, False if it
        already existed — callers use this to tell a fresh tree from an existing
        one (Phase 2 uses it to detect a stale git association)."""
        if self.dry_run:
            log.info("[dry-run] mkdir %s", path); return False
        resp = self.request("POST", self.ws_url("actions", "mkdir"),
                            body={"path": path, "description": None})
        if resp.status_code in (200, 201, 204):
            log.info("created directory %s", path)
            return True
        if resp.status_code == 409 or (resp.status_code == 400
                and "already exist" in (resp.text or "").lower()):
            log.info("directory %s already exists", path)
            return False
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
        resp = self.request("POST", self.ws_url("gitFolders"), body={
            "folderPath": _ws_relpath(folder_path), "gitRepositoryUrl": repo_url,
            "branchName": branch, "credentialKey": credential_key,
            "description": None, "gitProviderKey": None})
        if resp.status_code == 409 or (resp.status_code == 400
                and "already exist" in (resp.text or "").lower()):
            # A clone onto an already-associated path: almost always AIDP's stale
            # git association (folder gone but GIT_REPO record remains — see
            # docs/aidp-git-folder-issue.md). Fail with an actionable message
            # rather than a cryptic 409.
            raise RuntimeError(
                "create git folder {} -> HTTP {}: path already associated/exists. "
                "Likely a STALE git association (docs/aidp-git-folder-issue.md): remove "
                "the gitRepository server-side, or point git.parent_dir at a fresh path. "
                "Body: {}".format(folder_path, resp.status_code, resp.text))
        if resp.status_code not in (200, 201, 202, 204):
            raise RuntimeError("create git folder {} -> HTTP {}: {} (opc-request-id={})".format(
                folder_path, resp.status_code, resp.text, resp.headers.get("opc-request-id")))
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

    def get_git_repository(self, repo_key: str) -> Dict[str, Any]:
        """Fetch the connected git repository INCLUDING its credentialKey.
        shouldIncludeCredentialKey=true is REQUIRED — without it the key comes
        back null (verified against the workbench UI's HAR)."""
        return self.request_ok("GET", self.ws_url("gitRepositories", repo_key),
                               params={"shouldIncludeCredentialKey": "true"}).json()

    def reassociate_git_credential(self, repo_key: str, credential_key: str) -> None:
        """Re-point the git repository at *credential_key*. Mirrors the workbench
        UI's update-git-setting call exactly: PUT {credentialKey} only, which is
        synchronous (HTTP 204) — no async operation key, no other fields."""
        if self.dry_run:
            log.info("[dry-run] re-associate git repo %s -> credential %s",
                     repo_key, credential_key); return
        self.request_ok("PUT", self.ws_url("gitRepositories", repo_key),
                        body={"credentialKey": credential_key})
        log.info("re-associated git repo %s -> credential %s", repo_key, credential_key)

    def ensure_git_folder_credential(self, cfg: Dict[str, Any], repo_key: str) -> None:
        """Ensure an existing git folder's repository is associated with the
        credential owned by the RUNNING principal (resolve_git_credential_key
        resolves it in the caller's identity context). Per-principal credential
        ownership means a folder associated under another principal's credential
        (or none — credentialKey=null) can't be pulled here, so re-associate it."""
        desired = self.resolve_git_credential_key(cfg)
        current = self.get_git_repository(repo_key).get("credentialKey")
        if current == desired:
            log.info("git folder credential already correct (%s)", desired)
            return
        log.info("git folder credential %r != running principal's %r -> re-associating",
                 current, desired)
        self.reassociate_git_credential(repo_key, desired)

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

    # ---- Phase 3: bundle deploy ----
    def deploy_bundle(self, bundle_path: str) -> None:
        """Deploy the AIDP bundle at *bundle_path* (POST .../bundles/actions/deploy,
        async). The bundle definition creates/updates its own compute + workflow,
        so this reconcile no longer manages clusters/jobs individually."""
        if self.dry_run:
            log.info("[dry-run] deploy bundle %s", bundle_path); return
        # Bundle ops use the aiDataPlatforms surface (see bundle_ws_url).
        resp = self.request_ok("POST", self.bundle_ws_url("bundles", "actions", "deploy"),
                               body={"path": bundle_path})
        self._wait_if_async(resp)
        log.info("deployed bundle %s", bundle_path)

    # ---- Phase 4: ensure job runAs (workaround for CREATE dropping runAs) ----
    def ensure_jobs_run_as(self, cfg: Dict[str, Any], local_bundle_dir: str) -> None:
        """For each bundle job json that sets runAs, ensure the deployed job's
        runAs matches it.

        AIDP's job CREATE silently drops runAs (only UPDATE persists it — see
        JobModelConverter.getJobFromCreateDto), so a *fresh* bundle deploy leaves
        the job at runAs=null and its scheduled runs fail. We re-apply it via
        UpdateJob. Deployed jobs are namespaced `bundle_<name>_<uuid>`; match by
        that prefix against the json `name`."""
        import glob
        import os
        if self.dry_run:
            log.info("[dry-run] would ensure job runAs from %s/jobs/*.job.json",
                     local_bundle_dir)
            return
        job_dir = os.path.join(local_bundle_dir, "jobs")
        files = sorted(glob.glob(os.path.join(job_dir, "*.job.json")))
        if not files:
            log.warning("Phase 4: no job jsons found under %s — skipping", job_dir)
            return
        desired: Dict[str, str] = {}
        for jf in files:
            with open(jf) as f:
                jd = json.load(f)
            name, run_as = jd.get("name"), jd.get("runAs")
            if name and run_as:
                desired[name] = run_as
            elif name:
                log.info("Phase 4: job %r has no runAs in its json — nothing to enforce", name)
        if not desired:
            return
        all_jobs = self.list_all(self.ws_url("jobs"))
        for name, run_as in desired.items():
            prefix = "bundle_" + name + "_"
            matches = [j for j in all_jobs
                       if (j.get("name") or "").startswith(prefix) or j.get("name") == name]
            if not matches:
                raise RuntimeError(
                    "Phase 4: no deployed job found for bundle job {!r} (looked for "
                    "name {!r} or prefix {!r}) — did the bundle deploy succeed?".format(
                        name, name, prefix))
            if len(matches) > 1:
                log.warning("Phase 4: %d deployed jobs match %r; applying runAs to all",
                            len(matches), name)
            for j in matches:
                self._ensure_job_run_as(j["key"], run_as)

    def _ensure_job_run_as(self, job_key: str, run_as: str) -> None:
        """Set *job_key*'s runAs to *run_as* (if it differs) via a full-replace
        UpdateJob. Setting runAs needs ADMIN on the job; briefly retry a transient
        404 NotAuthorizedOrNotFound in case the creator's admin grant propagates
        asynchronously. *run_as* must be a credential KEY (UUID) — a display name
        404s the same way and will NOT clear with retries (see Phase 4 ticket)."""
        current = self.request_ok("GET", self.ws_url("jobs", job_key)).json()
        if current.get("runAs") == run_as:
            log.info("Phase 4: job %s runAs already %r", job_key, run_as)
            return
        log.info("Phase 4: job %s runAs=%r -> setting %r (bundle CREATE dropped it)",
                 job_key, current.get("runAs"), run_as)
        read_only = ("key", "createdBy", "createdByName", "updatedBy",
                     "updatedByName", "timeCreated", "timeUpdated")
        body = {k: v for k, v in current.items() if k not in read_only}
        body["runAs"] = run_as
        deadline = time.time() + self.runas_grant_timeout
        delay = 5.0
        while True:
            resp = self.request("PUT", self.ws_url("jobs", job_key), body=body)
            if resp.status_code in (200, 201, 202, 204):
                self._wait_if_async(resp)
                log.info("Phase 4: job %s runAs set to %r", job_key, run_as)
                return
            txt = resp.text or ""
            transient = (resp.status_code == 404
                         and "NotAuthorizedOrNotFound" in txt and "ADMIN" in txt)
            remaining = deadline - time.time()
            if not transient or remaining <= 0:
                raise RuntimeError(
                    "Phase 4: update job {} runAs -> HTTP {}: {} (opc-request-id={})".format(
                        job_key, resp.status_code, txt, resp.headers.get("opc-request-id")))
            wait = min(delay, remaining)
            log.info("Phase 4: job %s update got 404 ADMIN (admin grant not yet "
                     "propagated, or runAs is not a valid credential key); retrying "
                     "in %.0fs (%.0fs left)", job_key, wait, remaining)
            time.sleep(wait)
            delay = min(delay * 1.5, 30)


def phase0_credential(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 0: ensure git credential ==")
    if client.offline:
        log.info("[offline dry-run] would ensure git credential %r (from secret %s)",
                 cfg["git"]["credential_name"], cfg["git"]["credential_secret_id"]); return
    client.ensure_git_credential(cfg)


def phase1_directory(client: "AidpClient", cfg: Dict[str, Any]) -> bool:
    log.info("== Phase 1: ensure directory ==")
    # Return whether the parent was freshly created — Phase 2 uses it to spot a
    # stale git association (if the parent didn't exist, no git folder under it
    # can exist, so any isAssociated=true is stale).
    return client.ensure_directory(cfg["git"]["parent_dir"])


def phase2_git_folder(client: "AidpClient", cfg: Dict[str, Any],
                      parent_was_absent: bool = False) -> None:
    log.info("== Phase 2: git folder (create or pull) ==")
    g = cfg["git"]
    fp = resolve_folder_path(cfg)  # parent_dir + repo name, or AIDP_FOLDER_PATH override
    if client.offline:
        log.info("[offline dry-run] would create-or-pull git folder %s (%s@%s)",
                 fp, g["repository_url"], g["branch"]); return
    meta = client.git_folder_metadata(fp)
    associated = bool(meta.get("isAssociated") and meta.get("repoKey"))
    # Re-clone guard for AIDP's stale-association bug (docs/aidp-git-folder-issue.md):
    # if the parent dir was just created, the git folder under it cannot exist, so
    # an isAssociated=true is provably STALE. Pulling would leave a regular, partial
    # folder — clone instead (create_git_folder raises a clear error if the stale
    # association still blocks the clone).
    if associated and parent_was_absent:
        log.warning("git folder %s reports associated, but its parent was just created "
                    "-> stale association (docs/aidp-git-folder-issue.md); cloning instead", fp)
        associated = False
    if associated:
        # Ensure the folder is bound to the running principal's credential before
        # pulling — a credential owned by another principal (or none) can't be
        # resolved here and the pull would fail. Re-associate if it differs.
        client.ensure_git_folder_credential(cfg, meta["repoKey"])
        log.info("git folder exists; pulling %s", g["branch"])
        client.wait_for_async(client.git_pull(meta["repoKey"], fp, g["branch"]))
    else:
        log.info("git folder absent; cloning")
        cred_key = client.resolve_git_credential_key(cfg)
        client.wait_for_async(client.create_git_folder(
            fp, g["repository_url"], g["branch"], cred_key))


def phase3_bundle(client: "AidpClient", cfg: Dict[str, Any]) -> None:
    log.info("== Phase 3: deploy bundle ==")
    g = cfg["git"]
    # The bundle lives inside the cloned git folder at git.bundle_path.
    fp = resolve_folder_path(cfg)
    bundle_path = fp.rstrip("/") + "/" + g["bundle_path"].strip("/")
    if client.offline:
        log.info("[offline dry-run] would deploy bundle %s", bundle_path); return
    client.deploy_bundle(bundle_path)


def phase4_job_runas(client: "AidpClient", cfg: Dict[str, Any], config_path: str) -> None:
    log.info("== Phase 4: ensure job runAs ==")
    if client.offline:
        log.info("[offline dry-run] would ensure deployed jobs' runAs from bundle job jsons")
        return
    import os
    # The bundle's job jsons live in the local repo checkout (the runner's
    # workspace) at <repo>/<git.bundle_path>/jobs/*.job.json. Derive the repo root
    # from the config file's location (deploy/cicd.yaml -> repo root), overridable
    # via AIDP_BUNDLE_LOCAL_DIR.
    local_dir = os.environ.get("AIDP_BUNDLE_LOCAL_DIR")
    if not local_dir:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
        local_dir = os.path.join(repo_root, cfg["git"]["bundle_path"].strip("/"))
    client.ensure_jobs_run_as(cfg, local_dir)


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


def run(cfg: Dict[str, Any], dry_run: bool, runner: str = "vm",
        config_path: str = "") -> None:
    # --runner selects ONLY the AIDP signer (RUNNER_AUTH). argparse restricts the
    # value to the map keys, so a missing key is a programming error — fail loud.
    method = RUNNER_AUTH[runner]
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
        log.warning("No OCI principal (%s); offline dry-run: config only.", exc)
    client = AidpClient(cfg, signer, dry_run=dry_run)
    phase0_credential(client, cfg)
    parent_was_absent = phase1_directory(client, cfg)
    phase2_git_folder(client, cfg, parent_was_absent)
    phase3_bundle(client, cfg)
    phase4_job_runas(client, cfg, config_path)
    log.info("== Done ==")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AIDP CI/CD reconcile")
    parser.add_argument("--config", required=True)
    parser.add_argument("--runner", choices=sorted(RUNNER_AUTH), default="vm",
                        help="Which runner this executes on (vm|oke); selects ONLY "
                             "the AIDP signer (RUNNER_AUTH). Everything else is identical.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + log intended actions; mutate nothing.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.dry_run:
        log.info("DRY RUN — no mutations will be made.")
    run(cfg, dry_run=args.dry_run, runner=args.runner, config_path=args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
