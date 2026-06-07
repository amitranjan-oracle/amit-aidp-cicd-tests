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


if __name__ == "__main__":
    unittest.main()
