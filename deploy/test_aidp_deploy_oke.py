#!/usr/bin/env python3
"""Unit tests for the OKE additions to aidp_deploy (no OCI/network needed)."""
import unittest
from datetime import datetime, timezone

import aidp_deploy as A


class _Resp:
    def __init__(self, payload, headers=None, status_code=200, text=""):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def _client():
    cfg = {"aidp": {"region": "r", "data_lake_ocid": "d", "path_prefix": "p",
                    "api_version": "1", "workspace_key": "w"},
           "git": {"credential_name": "cicd-git-account",
                   "credential_secret_id": "ocid1.vaultsecret.oc1.iad.example",
                   "credential_username": "amitranjan-oracle"}}
    return A.AidpClient(cfg, signer=None), cfg


class SelectAuthMethod(unittest.TestCase):
    def test_default_is_instance_principal(self):
        self.assertEqual(A.select_auth_method({}), "instance_principal")

    def test_resource_principal_env(self):
        self.assertEqual(
            A.select_auth_method({"OCI_RESOURCE_PRINCIPAL_VERSION": "2.2"}),
            "resource_principal")

    def test_oke_wi_wins_over_rp(self):
        env = {"AIDP_AUTH_METHOD": "oke_workload_identity",
               "OCI_RESOURCE_PRINCIPAL_VERSION": "2.2"}
        self.assertEqual(A.select_auth_method(env), "oke_workload_identity")

    def test_oke_wi_case_insensitive(self):
        self.assertEqual(
            A.select_auth_method({"AIDP_AUTH_METHOD": "OKE_Workload_Identity"}),
            "oke_workload_identity")


class FindSettingKeyByName(unittest.TestCase):
    def test_found(self):
        settings = [{"name": "other", "key": "K0"},
                    {"name": "cicd-git-account", "key": "K1"}]
        self.assertEqual(A._find_setting_key_by_name(settings, "cicd-git-account"), "K1")

    def test_absent_returns_none(self):
        self.assertIsNone(A._find_setting_key_by_name([{"name": "x", "key": "K"}], "y"))


class ParseIso(unittest.TestCase):
    def test_z_with_millis(self):
        self.assertEqual(A._parse_iso("2026-05-08T22:11:19.646Z"),
                         datetime(2026, 5, 8, 22, 11, 19, 646000, tzinfo=timezone.utc))

    def test_z_no_frac(self):
        self.assertEqual(A._parse_iso("2026-06-01T00:00:00Z"),
                         datetime(2026, 6, 1, tzinfo=timezone.utc))

    def test_none_and_garbage(self):
        self.assertIsNone(A._parse_iso(None))
        self.assertIsNone(A._parse_iso("not-a-time"))


class ResolveGitCredentialKey(unittest.TestCase):
    def test_resolves_by_config_name(self):
        client, cfg = _client()
        client.list_git_account_settings = lambda: [
            {"name": "cicd-git-account", "key": "WI_KEY"}]
        self.assertEqual(client.resolve_git_credential_key(cfg), "WI_KEY")

    def test_absent_raises(self):
        client, cfg = _client()
        client.list_git_account_settings = lambda: [{"name": "x", "key": "K"}]
        with self.assertRaises(RuntimeError):
            client.resolve_git_credential_key(cfg)

    def test_absent_in_dry_run_returns_none(self):
        # dry-run phase 0 doesn't create the credential, so a first-run absence
        # must not fail the resolve (downstream clone/pull are dry-run no-ops).
        client, cfg = _client()
        client.dry_run = True
        client.list_git_account_settings = lambda: [{"name": "x", "key": "K"}]
        self.assertIsNone(client.resolve_git_credential_key(cfg))


class EnsureGitCredential(unittest.TestCase):
    def _wire(self, client, existing=None, secret=None, secret_exc=None):
        client._find_setting_by_name = lambda name: existing
        if secret_exc is not None:
            def _raise(*a, **k):
                raise secret_exc
            client._read_secret = _raise
        else:
            client._read_secret = lambda sid, comp: secret
        self.deleted = []
        client.delete_user_setting = lambda key: self.deleted.append(key)
        self.created = {}

        def _create(method, url, body=None, **k):
            self.created["body"] = body
            return _Resp({"key": "NEW"})
        client.request_ok = _create

    def test_absent_creates_from_secret(self):
        client, cfg = _client()
        self._wire(client, existing=None,
                   secret=("TOKEN", datetime(2026, 6, 1, tzinfo=timezone.utc)))
        self.assertEqual(client.ensure_git_credential(cfg), "NEW")
        self.assertEqual(self.created["body"]["data"]["personalAccessToken"], "TOKEN")
        self.assertEqual(self.deleted, [])

    def test_present_rotated_recreates(self):
        client, cfg = _client()
        existing = {"name": "cicd-git-account", "key": "OLD",
                    "timeUpdated": "2026-05-01T00:00:00Z"}
        self._wire(client, existing=existing,
                   secret=("TOKEN", datetime(2026, 6, 1, tzinfo=timezone.utc)))  # newer
        self.assertEqual(client.ensure_git_credential(cfg), "NEW")
        self.assertEqual(self.deleted, ["OLD"])  # recreated

    def test_present_not_rotated_noop(self):
        client, cfg = _client()
        existing = {"name": "cicd-git-account", "key": "OLD",
                    "timeUpdated": "2026-06-01T00:00:00Z"}
        self._wire(client, existing=existing,
                   secret=("TOKEN", datetime(2026, 5, 1, tzinfo=timezone.utc)))  # older
        self.assertEqual(client.ensure_git_credential(cfg), "OLD")
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.created, {})  # no create

    def test_secret_unreadable_keeps_existing(self):
        client, cfg = _client()
        existing = {"name": "cicd-git-account", "key": "OLD",
                    "timeUpdated": "2026-06-01T00:00:00Z"}
        self._wire(client, existing=existing, secret_exc=RuntimeError("401 NotAuthorized"))
        self.assertEqual(client.ensure_git_credential(cfg), "OLD")
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.created, {})

    def test_secret_unreadable_and_absent_raises(self):
        client, cfg = _client()
        self._wire(client, existing=None, secret_exc=RuntimeError("401 NotAuthorized"))
        with self.assertRaises(RuntimeError):
            client.ensure_git_credential(cfg)


class DerivedConfig(unittest.TestCase):
    def test_api_version_map(self):
        self.assertEqual(A.default_api_version_for("dataLakes"), "20240831")
        self.assertEqual(A.default_api_version_for("aiDataPlatforms"), "20260430")
        self.assertEqual(A.default_api_version_for("other"), A.DEFAULT_API_VERSION)

    def test_client_derives_api_version_when_absent(self):
        cfg = {"aidp": {"region": "r", "data_lake_ocid": "d", "path_prefix": "dataLakes",
                        "workspace_key": "w"}, "git": {}}
        self.assertEqual(A.AidpClient(cfg, signer=None).api_version, "20240831")

    def test_client_api_version_override(self):
        cfg = {"aidp": {"region": "r", "data_lake_ocid": "d", "path_prefix": "dataLakes",
                        "api_version": "99999999", "workspace_key": "w"}, "git": {}}
        self.assertEqual(A.AidpClient(cfg, signer=None).api_version, "99999999")

    def test_folder_path_derived_from_repo(self):
        import os
        os.environ.pop("AIDP_FOLDER_PATH", None)
        cfg = {"git": {"repository_url": "https://github.com/x/my-repo.git",
                       "parent_dir": "/Workspace/cicd_folder"}}
        self.assertEqual(A.resolve_folder_path(cfg), "/Workspace/cicd_folder/my-repo")

    def test_folder_path_env_override(self):
        import os
        os.environ["AIDP_FOLDER_PATH"] = "/Workspace/cicd_folder/my-repo-x"
        try:
            cfg = {"git": {"repository_url": "https://github.com/x/my-repo.git",
                           "parent_dir": "/Workspace/cicd_folder"}}
            self.assertEqual(A.resolve_folder_path(cfg), "/Workspace/cicd_folder/my-repo-x")
        finally:
            os.environ.pop("AIDP_FOLDER_PATH", None)


class RunnerProfile(unittest.TestCase):
    def test_folder_path_is_runner_independent(self):
        # VM and OKE share one folder now — no per-runner suffix.
        import os
        os.environ.pop("AIDP_FOLDER_PATH", None)
        cfg = {"git": {"repository_url": "https://github.com/x/my-repo.git",
                       "parent_dir": "/Workspace/cicd_folder"}}
        self.assertEqual(A.resolve_folder_path(cfg), "/Workspace/cicd_folder/my-repo")

    def test_runner_auth_map(self):
        # --runner selects ONLY the signer; both runners use the host instance
        # principal (WI is unusable for AIDP volume/list ops). No folder suffix.
        self.assertEqual(A.RUNNER_AUTH, {"vm": "instance_principal",
                                         "oke": "instance_principal"})
        self.assertFalse(hasattr(A, "RUNNER_FOLDER_SUFFIX"))

    def test_build_signer_honors_explicit_method(self):
        # method=None falls back to env selection; an explicit method overrides it.
        import os
        captured = {}

        class _FakeOCI:
            class auth:
                class signers:
                    @staticmethod
                    def get_oke_workload_identity_resource_principal_signer():
                        captured["m"] = "wi"; return "WI_SIGNER"

                    @staticmethod
                    def get_resource_principals_signer():
                        captured["m"] = "rp"; return "RP_SIGNER"

                    class InstancePrincipalsSecurityTokenSigner:
                        def __init__(self):
                            captured["m"] = "ip"

        import sys
        from unittest import mock
        # patch.dict restores any pre-existing real `oci` module after the test
        with mock.patch.dict(sys.modules, {"oci": _FakeOCI}):
            try:
                # explicit method wins even if env says otherwise
                os.environ["AIDP_AUTH_METHOD"] = "oke_workload_identity"
                self.assertEqual(A.build_signer("resource_principal"), "RP_SIGNER")
                self.assertEqual(captured["m"], "rp")
                # method=None -> env selection (WI)
                self.assertEqual(A.build_signer(), "WI_SIGNER")
                self.assertEqual(captured["m"], "wi")
            finally:
                os.environ.pop("AIDP_AUTH_METHOD", None)


class ListAllPagination(unittest.TestCase):
    def test_follows_opc_next_page(self):
        client, _ = _client()
        calls = []
        pages = [
            (_Resp({"items": [{"name": "a"}, {"name": "b"}]}, {"opc-next-page": "P2"})),
            (_Resp({"items": [{"name": "c"}]}, {})),  # last page: no opc-next-page
        ]

        def fake_request_ok(method, url, body=None, params=None, **k):
            calls.append(dict(params or {}))
            return pages[len(calls) - 1]

        client.request_ok = fake_request_ok
        items = client.list_all("http://x/jobs")
        self.assertEqual([i["name"] for i in items], ["a", "b", "c"])  # all pages merged
        self.assertEqual(calls[0], {})                # page 1: no page token
        self.assertEqual(calls[1], {"page": "P2"})    # page 2: uses the header token

    def test_preserves_initial_params_across_pages(self):
        client, _ = _client()
        seen = []
        pages = [_Resp({"items": [{"k": 1}]}, {"opc-next-page": "N"}),
                 _Resp({"items": [{"k": 2}]}, {})]

        def fake(method, url, body=None, params=None, **k):
            seen.append(dict(params or {}))
            return pages[len(seen) - 1]

        client.request_ok = fake
        client.list_all("http://x/userSettings", params={"settingType": "GIT_ACCOUNT"})
        self.assertEqual(seen[0], {"settingType": "GIT_ACCOUNT"})
        self.assertEqual(seen[1], {"settingType": "GIT_ACCOUNT", "page": "N"})

    def test_single_page_no_header(self):
        client, _ = _client()
        client.request_ok = lambda *a, **k: _Resp({"items": [{"name": "x"}]}, {})
        self.assertEqual(client.list_all("http://x"), [{"name": "x"}])

    def test_bare_list_payload(self):
        client, _ = _client()
        client.request_ok = lambda *a, **k: _Resp([{"name": "x"}], {})
        self.assertEqual(client.list_all("http://x"), [{"name": "x"}])

    def test_empty_page_stops(self):
        client, _ = _client()
        # a stray opc-next-page with an empty page must not loop forever
        client.request_ok = lambda *a, **k: _Resp({"items": []}, {"opc-next-page": "Z"})
        self.assertEqual(client.list_all("http://x"), [])

    def test_raises_on_non_list_page(self):
        client, _ = _client()
        # a dict without "items" is a malformed list response -> fail loud
        client.request_ok = lambda *a, **k: _Resp({"unexpected": "shape"}, {})
        with self.assertRaises(RuntimeError):
            client.list_all("http://x/jobs")


class EnsureDirectory(unittest.TestCase):
    def test_created_returns_true(self):
        client, _ = _client()
        client.request = lambda *a, **k: _Resp({}, status_code=201)
        self.assertTrue(client.ensure_directory("/Workspace/x"))

    def test_already_exists_returns_false(self):
        client, _ = _client()
        client.request = lambda *a, **k: _Resp({}, status_code=409, text="already exists")
        self.assertFalse(client.ensure_directory("/Workspace/x"))

    def test_400_already_exists_returns_false(self):
        client, _ = _client()
        client.request = lambda *a, **k: _Resp({}, status_code=400, text="folder already exists")
        self.assertFalse(client.ensure_directory("/Workspace/x"))

    def test_400_other_exist_error_raises(self):
        # "parent does not exist" contains "exist" but not "already exist" -> not
        # an already-exists case, must surface as an error (tightened guard).
        client, _ = _client()
        client.request = lambda *a, **k: _Resp({}, status_code=400, text="parent does not exist")
        with self.assertRaises(RuntimeError):
            client.ensure_directory("/Workspace/x")


class CreateGitFolderConflict(unittest.TestCase):
    def test_create_git_folder_409_raises_actionable(self):
        client, _ = _client()
        client.request = lambda *a, **k: _Resp({}, status_code=409, text="already exists")
        with self.assertRaises(RuntimeError) as ctx:
            client.create_git_folder("/Workspace/x/repo", "https://h/r.git", "main", "CK")
        self.assertIn("STALE", str(ctx.exception))


class BundleDeploy(unittest.TestCase):
    def test_deploy_bundle_posts_to_actions_deploy(self):
        client, _ = _client()
        seen = {}

        def fake_ok(method, url, body=None, params=None, **k):
            seen.update(method=method, url=url, body=body)
            return _Resp({}, status_code=202)  # no async-key header -> no wait

        client.request_ok = fake_ok
        client.deploy_bundle("/Workspace/x/bundle")
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].endswith("/bundles/actions/deploy"))
        # bundle ops use the aiDataPlatforms/20260430 surface, not dataLakes/20240831
        self.assertIn("/aiDataPlatforms/", seen["url"])
        self.assertIn("/20260430/", seen["url"])
        self.assertEqual(seen["body"], {"path": "/Workspace/x/bundle"})

    def test_phase3_builds_bundle_path_and_deploys(self):
        import os
        os.environ.pop("AIDP_FOLDER_PATH", None)
        client, cfg = _client()
        cfg["git"]["repository_url"] = "https://github.com/x/repo.git"
        cfg["git"]["parent_dir"] = "/Workspace/cicd_test_01"
        cfg["git"]["bundle_path"] = "bundle"
        client.offline = False
        seen = []
        client.deploy_bundle = lambda p: seen.append(p)
        A.phase3_bundle(client, cfg)
        # <parent_dir>/<repo>/<bundle_path>
        self.assertEqual(seen, ["/Workspace/cicd_test_01/repo/bundle"])


class Phase2StaleAssociationGuard(unittest.TestCase):
    def _client_for_phase2(self):
        client, cfg = _client()
        cfg["git"]["repository_url"] = "https://github.com/x/repo.git"
        cfg["git"]["parent_dir"] = "/Workspace/cicd_test_01"
        cfg["git"]["branch"] = "main"
        client.offline = False
        client.git_folder_metadata = lambda fp: {"isAssociated": True, "repoKey": "R"}
        client.resolve_git_credential_key = lambda c: "CK"
        client.wait_for_async = lambda k: None
        self.calls = []
        client.git_pull = lambda *a, **k: self.calls.append("pull")
        client.create_git_folder = lambda *a, **k: self.calls.append("clone")
        client.ensure_git_folder_credential = lambda *a, **k: None  # tested separately
        return client, cfg

    def test_associated_but_parent_absent_clones(self):
        client, cfg = self._client_for_phase2()
        A.phase2_git_folder(client, cfg, parent_was_absent=True)
        self.assertEqual(self.calls, ["clone"])  # stale association -> clone, not pull

    def test_associated_and_parent_existed_pulls(self):
        client, cfg = self._client_for_phase2()
        A.phase2_git_folder(client, cfg, parent_was_absent=False)
        self.assertEqual(self.calls, ["pull"])  # healthy association -> pull


class GitRepositoryCredential(unittest.TestCase):
    def test_get_requests_credential_key(self):
        client, _ = _client()
        seen = {}

        def fake_ok(method, url, body=None, params=None, **k):
            seen.update(method=method, url=url, params=params)
            return _Resp({"credentialKey": "K", "gitUrl": "u"})

        client.request_ok = fake_ok
        out = client.get_git_repository("R")
        self.assertEqual(out["credentialKey"], "K")
        self.assertEqual(seen["method"], "GET")
        self.assertTrue(seen["url"].endswith("/gitRepositories/R"))
        # shouldIncludeCredentialKey=true is required or the key comes back null
        self.assertEqual(seen["params"], {"shouldIncludeCredentialKey": "true"})

    def test_reassociate_puts_credential_key_only(self):
        client, _ = _client()
        seen = {}

        def fake_ok(method, url, body=None, params=None, **k):
            seen.update(method=method, url=url, body=body)
            return _Resp({}, status_code=204)  # UI: 204 No Content, synchronous

        client.request_ok = fake_ok
        client.reassociate_git_credential("R", "NEWKEY")
        self.assertEqual(seen["method"], "PUT")
        self.assertTrue(seen["url"].endswith("/gitRepositories/R"))
        self.assertEqual(seen["body"], {"credentialKey": "NEWKEY"})  # matches HAR


class EnsureGitFolderCredential(unittest.TestCase):
    def _wire(self, current_key, desired_key="MINE"):
        client, cfg = _client()
        client.resolve_git_credential_key = lambda c: desired_key
        client.get_git_repository = lambda rk: {"credentialKey": current_key}
        self.reassoc = []
        client.reassociate_git_credential = lambda rk, ck: self.reassoc.append((rk, ck))
        return client, cfg

    def test_noop_when_matches(self):
        client, cfg = self._wire(current_key="MINE")
        client.ensure_git_folder_credential(cfg, "R")
        self.assertEqual(self.reassoc, [])  # already correct -> no PUT

    def test_reassociates_when_other_principal(self):
        client, cfg = self._wire(current_key="OTHER")
        client.ensure_git_folder_credential(cfg, "R")
        self.assertEqual(self.reassoc, [("R", "MINE")])

    def test_reassociates_when_null(self):
        client, cfg = self._wire(current_key=None)  # HAR showed credentialKey can be null
        client.ensure_git_folder_credential(cfg, "R")
        self.assertEqual(self.reassoc, [("R", "MINE")])


if __name__ == "__main__":
    unittest.main()
