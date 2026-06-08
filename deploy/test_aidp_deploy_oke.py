#!/usr/bin/env python3
"""Unit tests for the OKE additions to aidp_deploy (no OCI/network needed)."""
import unittest
from datetime import datetime, timezone

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


if __name__ == "__main__":
    unittest.main()
