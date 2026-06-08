#!/usr/bin/env python3
"""Unit tests for the OKE additions to aidp_deploy (no OCI/network needed)."""
import unittest

import aidp_deploy as A


class _Resp:
    def __init__(self, payload):
        self._payload = payload

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


class EnsureGitCredential(unittest.TestCase):
    def test_existing_returns_key_without_reading_secret(self):
        client, cfg = _client()
        client.list_git_account_settings = lambda: [
            {"name": "cicd-git-account", "key": "OLD"}]
        client._read_secret_pat = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not read secret when credential exists"))
        self.assertEqual(client.ensure_git_credential(cfg), "OLD")

    def test_absent_reads_secret_and_creates(self):
        client, cfg = _client()
        client.list_git_account_settings = lambda: []
        seen = {}
        client._read_secret_pat = lambda sid, comp: seen.setdefault("pat", "TOKEN")
        captured = {}

        def fake_request_ok(method, url, body=None, **k):
            captured["method"] = method
            captured["body"] = body
            return _Resp({"key": "NEW"})

        client.request_ok = fake_request_ok
        self.assertEqual(client.ensure_git_credential(cfg), "NEW")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["data"]["personalAccessToken"], "TOKEN")
        self.assertEqual(captured["body"]["data"]["type"], "GIT_ACCOUNT")
        self.assertEqual(captured["body"]["name"], "cicd-git-account")


if __name__ == "__main__":
    unittest.main()
