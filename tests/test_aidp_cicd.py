import json
import os
import tempfile
import unittest

import aidp_cicd


class TestConfigLoading(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_load_config_ok(self):
        path = self._write(
            "aidp:\n"
            "  region: us-ashburn-1\n"
            "  data_lake_ocid: ocid1.datalake.x\n"
            "  path_prefix: dataLakes\n"
            "  api_version: '20240831'\n"
            "  workspace_key: ws-1\n"
            "git:\n"
            "  repository_url: https://example/repo.git\n"
            "  branch: main\n"
            "  credential_key: cred-1\n"
            "  parent_dir: /Workspace/cicd_folder\n"
            "  folder_path: /Workspace/cicd_folder/aidp-tests\n"
            "compute:\n  name: ephemeral_01\n  spec_file: specs/c.json\n"
            "workflow:\n  name: cicd_workflow_job\n  spec_file: specs/j.json\n  cluster_name: ephemeral_01\n"
        )
        cfg = aidp_cicd.load_config(path)
        self.assertEqual(cfg["aidp"]["region"], "us-ashburn-1")
        self.assertEqual(cfg["workflow"]["cluster_name"], "ephemeral_01")

    def test_load_config_missing_keys(self):
        path = self._write("aidp:\n  region: us-ashburn-1\n")
        with self.assertRaises(ValueError) as ctx:
            aidp_cicd.load_config(path)
        self.assertIn("git.repository_url", str(ctx.exception))
        self.assertNotIn("aidp.region", str(ctx.exception))      # present key excluded
        self.assertIn("aidp.data_lake_ocid", str(ctx.exception)) # another missing key reported

    def test_load_config_whitespace_value_rejected(self):
        path = self._write(
            "aidp:\n"
            "  region: '   '\n"
            "  data_lake_ocid: ocid1.datalake.x\n"
            "  path_prefix: dataLakes\n"
            "  api_version: '20240831'\n"
            "  workspace_key: ws-1\n"
            "git:\n"
            "  repository_url: https://example/repo.git\n"
            "  branch: main\n"
            "  credential_key: cred-1\n"
            "  parent_dir: /Workspace/cicd_folder\n"
            "  folder_path: /Workspace/cicd_folder/aidp-tests\n"
            "compute:\n  name: ephemeral_01\n  spec_file: specs/c.json\n"
            "workflow:\n  name: cicd_workflow_job\n  spec_file: specs/j.json\n  cluster_name: ephemeral_01\n"
        )
        with self.assertRaises(ValueError) as ctx:
            aidp_cicd.load_config(path)
        self.assertIn("aidp.region", str(ctx.exception))

    def test_load_config_empty_file_raises(self):
        path = self._write("")
        with self.assertRaises(ValueError):
            aidp_cicd.load_config(path)

    def test_load_spec(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"displayName": "ephemeral_01"}, f)
        self.addCleanup(os.remove, path)
        self.assertEqual(aidp_cicd.load_spec(path)["displayName"], "ephemeral_01")


class TestSubsetDiff(unittest.TestCase):
    def test_satisfies_scalar_and_nested(self):
        desired = {"a": 1, "b": {"c": 2}}
        live = {"a": 1, "b": {"c": 2, "extra": 9}, "server": "x"}
        self.assertTrue(aidp_cicd.satisfies(desired, live))   # extras ignored

    def test_satisfies_detects_change(self):
        self.assertFalse(aidp_cicd.satisfies({"a": 1}, {"a": 2}))
        self.assertFalse(aidp_cicd.satisfies({"a": 1}, {}))           # missing key

    def test_satisfies_lists_elementwise_subset(self):
        desired = {"t": [{"k": "x"}]}
        live = {"t": [{"k": "x", "extra": 1}]}
        self.assertTrue(aidp_cicd.satisfies(desired, live))
        self.assertFalse(aidp_cicd.satisfies({"t": [{"k": "x"}]}, {"t": []}))

    def test_cluster_satisfied_ignores_volatile(self):
        desired = {"displayName": "ephemeral_01",
                   "driverConfig": {"driverShape": "amd.generic"}}
        live = {"displayName": "ephemeral_01", "key": "abc", "state": "ACTIVE",
                "driverConfig": {"driverShape": "amd.generic", "driverNodeType": None},
                "jdbcEndpointUrl": "jdbc:..."}
        self.assertTrue(aidp_cicd.cluster_in_sync(desired, live))

    def test_job_compares_cluster_by_name_not_key(self):
        desired = {"name": "cicd_workflow_job",
                   "jobClusters": [{"clusterName": "ephemeral_01"}]}
        live = {"name": "cicd_workflow_job", "key": "j1",
                "jobClusters": [{"clusterName": "ephemeral_01",
                                 "clusterKey": "a3ad", "newCluster": None}]}
        self.assertTrue(aidp_cicd.job_in_sync(desired, live))


if __name__ == "__main__":
    unittest.main()
