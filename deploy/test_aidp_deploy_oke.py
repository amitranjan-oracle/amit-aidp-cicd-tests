#!/usr/bin/env python3
"""Unit tests for the OKE additions to aidp_deploy (no OCI/network needed)."""
import os
import unittest

import aidp_deploy as A


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
                    {"name": "cicd-workload-principal", "key": "K1"}]
        self.assertEqual(
            A._find_setting_key_by_name(settings, "cicd-workload-principal"), "K1")

    def test_absent_returns_none(self):
        self.assertIsNone(A._find_setting_key_by_name([{"name": "x", "key": "K"}], "y"))


class ResolveGitCredentialKey(unittest.TestCase):
    def _client(self):
        cfg = {"aidp": {"region": "r", "data_lake_ocid": "d", "path_prefix": "p",
                        "api_version": "1", "workspace_key": "w"},
               "git": {"credential_key": "YAML_KEY"}}
        return A.AidpClient(cfg, signer=None), cfg

    def setUp(self):
        os.environ.pop("AIDP_GIT_CREDENTIAL_NAME", None)

    def tearDown(self):
        os.environ.pop("AIDP_GIT_CREDENTIAL_NAME", None)

    def test_no_env_uses_yaml_key(self):
        client, cfg = self._client()
        client.list_git_account_settings = lambda: (_ for _ in ()).throw(
            AssertionError("must not query when env unset"))
        self.assertEqual(client.resolve_git_credential_key(cfg), "YAML_KEY")

    def test_env_resolves_by_name(self):
        client, cfg = self._client()
        os.environ["AIDP_GIT_CREDENTIAL_NAME"] = "cicd-workload-principal"
        client.list_git_account_settings = lambda: [
            {"name": "cicd-workload-principal", "key": "WI_KEY"}]
        self.assertEqual(client.resolve_git_credential_key(cfg), "WI_KEY")

    def test_env_name_missing_raises(self):
        client, cfg = self._client()
        os.environ["AIDP_GIT_CREDENTIAL_NAME"] = "absent"
        client.list_git_account_settings = lambda: [{"name": "x", "key": "K"}]
        with self.assertRaises(RuntimeError):
            client.resolve_git_credential_key(cfg)


if __name__ == "__main__":
    unittest.main()
