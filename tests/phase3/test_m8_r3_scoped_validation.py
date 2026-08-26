"""
Targeted tests for M8 R3 scoped validation semantics.

Tests the corrected R3 invariant: M8 observation must not create/delete/enable/disable/
reschedule/repoint or otherwise semantically mutate scheduler jobs/configuration;
unrelated runtime-state updates from concurrent jobs must not cause R3 failure.

Test cases:
  1. No jobs.json change => PASS
  2. Only allowed volatile/runtime fields change => PASS
  3. Unrelated job runtime metadata changes => PASS
  4. M8 or any job semantic/config field mutation => FAIL
  5. Job added => FAIL
  6. Job deleted => FAIL
  7. Enable/disable mutation => FAIL
  8. Schedule mutation => FAIL
  9. Script/delivery/config mutation => FAIL
  10. Malformed/unreadable jobs state => fail safely
"""

import unittest
import json
import os
import sys
import tempfile
import copy

# Add the scripts directory to path so we can import from m8_daily_observation
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
sys.path.insert(0, SCRIPTS_DIR)

from m8_daily_observation import (
    capture_jobs_signature,
    compare_jobs_signatures,
    VOLATILE_JOB_FIELDS,
)


def make_test_jobs():
    """Create a minimal jobs.json structure for testing."""
    return {
        "updated_at": "2026-08-18T22:00:00+08:00",
        "jobs": [
            {
                "id": "abc123",
                "name": "morning-brief",
                "enabled": True,
                "schedule": {"kind": "daily", "hour": 8, "minute": 30},
                "script": "morning_brief.py",
                "no_agent": False,
                "prompt": "Generate morning brief",
                "model": "gpt-4",
                "provider": "openai",
                "deliver": "telegram:5132341473",
                "workdir": "/home/ubuntu",
                "created_at": "2026-05-27T01:23:38+08:00",
                # Volatile fields
                "last_run_at": "2026-08-18T08:30:05+08:00",
                "last_status": "ok",
                "state": "scheduled",
                "next_run_at": "2026-08-19T08:30:00+08:00",
                "fire_claim": None,
                "last_error": None,
                "last_delivery_error": None,
                "paused_at": None,
                "paused_reason": None,
                "schedule_display": "daily at 08:30",
            },
            {
                "id": "def456",
                "name": "m8-restarted-day3-2026-08-19",
                "enabled": True,
                "schedule": {"kind": "once", "run_at": "2026-08-19T01:00:00+00:00"},
                "script": "m8_daily_observation.py",
                "no_agent": True,
                "model": None,
                "provider": None,
                "deliver": "local",
                "workdir": None,
                "created_at": "2026-08-17T10:00:00+08:00",
                # Volatile fields
                "last_run_at": None,
                "last_status": None,
                "state": "scheduled",
                "next_run_at": "2026-08-19T01:00:00+00:00",
                "fire_claim": None,
            },
        ],
    }


def write_jobs(tmpdir, data):
    """Write jobs.json to a temp directory."""
    path = os.path.join(tmpdir, "jobs.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


class TestR3NoChange(unittest.TestCase):
    """Test case 1: No jobs.json change => PASS."""

    def test_identical_signatures(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)
            sig_after = capture_jobs_signature(path)
            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertTrue(safe, f"Expected PASS but got: {changes}")
            self.assertIn("No semantic changes", changes)


class TestR3VolatileOnlyChange(unittest.TestCase):
    """Test case 2: Only allowed volatile/runtime fields change => PASS."""

    def test_volatile_field_changes_pass(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            # Mutate ONLY volatile fields
            data2 = copy.deepcopy(data)
            data2["updated_at"] = "2026-08-18T23:00:00+08:00"
            data2["jobs"][0]["last_run_at"] = "2026-08-18T22:30:00+08:00"
            data2["jobs"][0]["last_status"] = "ok"
            data2["jobs"][0]["state"] = "running"
            data2["jobs"][0]["next_run_at"] = "2026-08-20T08:30:00+08:00"
            data2["jobs"][0]["fire_claim"] = "claim-123"
            data2["jobs"][0]["schedule_display"] = "daily at 08:31"
            data2["jobs"][1]["last_run_at"] = "2026-08-19T01:00:05+08:00"
            data2["jobs"][1]["last_status"] = "ok"
            data2["jobs"][1]["state"] = "expired"
            data2["jobs"][1]["next_run_at"] = None

            path2 = write_jobs(tmpdir, data2)
            # Overwrite the same file
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertTrue(safe, f"Expected PASS for volatile-only changes but got: {changes}")

    def test_all_volatile_fields_change(self):
        """Every volatile field changes on every job — should still PASS."""
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            for j in data2["jobs"]:
                for vf in VOLATILE_JOB_FIELDS:
                    if vf in j:
                        j[vf] = f"changed_{vf}_value"
            data2["updated_at"] = "2026-08-19T00:00:00+08:00"

            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertTrue(safe, f"Expected PASS but got: {changes}")


class TestR3UnrelatedJobMetadataChange(unittest.TestCase):
    """Test case 3: Unrelated job runtime metadata changes => PASS."""

    def test_other_job_volatile_changes(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            # Only change job 0 (non-M8) volatile fields
            data2 = copy.deepcopy(data)
            data2["jobs"][0]["last_run_at"] = "2026-08-19T08:30:10+08:00"
            data2["jobs"][0]["last_status"] = "ok"
            data2["jobs"][0]["state"] = "scheduled"
            data2["jobs"][0]["next_run_at"] = "2026-08-20T08:30:00+08:00"

            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertTrue(safe, f"Expected PASS but got: {changes}")


class TestR3SemanticMutationFails(unittest.TestCase):
    """Test case 4: Semantic/config field mutation => FAIL."""

    def test_name_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["name"] = "tampered-brief"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for name change")
            self.assertIn("name", changes)

    def test_prompt_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["prompt"] = "MALICIOUS PROMPT INJECTION"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for prompt change")

    def test_model_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["model"] = "attacker-model"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for model change")

    def test_provider_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["provider"] = "attacker-provider"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for provider change")


class TestR3JobAdded(unittest.TestCase):
    """Test case 5: Job added => FAIL."""

    def test_job_added_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"].append({
                "id": "new-suspicious-job",
                "name": "exfil",
                "enabled": True,
                "script": "exfil.py",
            })
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for job added")
            self.assertIn("added", changes.lower())


class TestR3JobDeleted(unittest.TestCase):
    """Test case 6: Job deleted => FAIL."""

    def test_job_deleted_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"].pop()  # Remove last job
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for job deleted")
            self.assertIn("deleted", changes.lower())


class TestR3EnableDisableMutation(unittest.TestCase):
    """Test case 7: Enable/disable mutation => FAIL."""

    def test_disable_job_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["enabled"] = False
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for enable->disable")
            self.assertIn("enabled", changes)

    def test_enable_job_fails(self):
        data = make_test_jobs()
        data["jobs"][0]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["enabled"] = True
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for disable->enable")
            self.assertIn("enabled", changes)


class TestR3ScheduleMutation(unittest.TestCase):
    """Test case 8: Schedule mutation => FAIL."""

    def test_schedule_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["schedule"] = {"kind": "daily", "hour": 3, "minute": 0}
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for schedule change")
            self.assertIn("schedule", changes)


class TestR3ScriptDeliveryConfigMutation(unittest.TestCase):
    """Test case 9: Script/delivery/config mutation => FAIL."""

    def test_script_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][1]["script"] = "tampered_script.py"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for script change")
            self.assertIn("script", changes)

    def test_deliver_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["deliver"] = "telegram:attacker_chat_id"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for deliver change")
            self.assertIn("deliver", changes)

    def test_workdir_change_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][0]["workdir"] = "/tmp/attacker"
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for workdir change")
            self.assertIn("workdir", changes)

    def test_no_agent_flip_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)

            data2 = copy.deepcopy(data)
            data2["jobs"][1]["no_agent"] = False
            with open(path, "w") as f:
                json.dump(data2, f)
            sig_after = capture_jobs_signature(path)

            safe, changes = compare_jobs_signatures(sig_before, sig_after)
            self.assertFalse(safe, "Expected FAIL for no_agent flip")
            self.assertIn("no_agent", changes)


class TestR3MalformedUnreadable(unittest.TestCase):
    """Test case 10: Malformed/unreadable jobs state => fail safely."""

    def test_malformed_json_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "jobs.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            sig = capture_jobs_signature(path)
            self.assertIsNone(sig, "Malformed JSON should return None")

    def test_missing_file_fails(self):
        sig = capture_jobs_signature("/nonexistent/path/jobs.json")
        self.assertIsNone(sig, "Missing file should return None")

    def test_none_signature_compares_unsafe(self):
        safe, changes = compare_jobs_signatures(None, None)
        self.assertFalse(safe, "None signatures should be unsafe")
        self.assertIn("unreadable", changes.lower())

    def test_none_before_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_after = capture_jobs_signature(path)
            safe, changes = compare_jobs_signatures(None, sig_after)
            self.assertFalse(safe)

    def test_none_after_fails(self):
        data = make_test_jobs()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_jobs(tmpdir, data)
            sig_before = capture_jobs_signature(path)
            safe, changes = compare_jobs_signatures(sig_before, None)
            self.assertFalse(safe)

    def test_non_dict_data_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "jobs.json")
            with open(path, "w") as f:
                json.dump([1, 2, 3], f)  # list, not dict
            sig = capture_jobs_signature(path)
            self.assertIsNone(sig)

    def test_jobs_not_list_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "jobs.json")
            with open(path, "w") as f:
                json.dump({"jobs": "not a list"}, f)
            sig = capture_jobs_signature(path)
            self.assertIsNone(sig)


class TestR3RealJobsJson(unittest.TestCase):
    """Integration test against the real jobs.json file."""

    REAL_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")

    def test_real_jobs_json_parses(self):
        if not os.path.exists(self.REAL_PATH):
            self.skipTest("Real jobs.json not available")
        sig = capture_jobs_signature(self.REAL_PATH)
        self.assertIsNotNone(sig, "Real jobs.json should parse successfully")
        self.assertGreater(len(sig["job_ids"]), 0)

    def test_real_jobs_json_self_compare(self):
        if not os.path.exists(self.REAL_PATH):
            self.skipTest("Real jobs.json not available")
        sig = capture_jobs_signature(self.REAL_PATH)
        safe, changes = compare_jobs_signatures(sig, sig)
        self.assertTrue(safe, f"Self-compare should be safe: {changes}")

    def test_real_jobs_json_volatile_fields_excluded(self):
        """Verify that volatile fields are not in the scoped signature."""
        if not os.path.exists(self.REAL_PATH):
            self.skipTest("Real jobs.json not available")
        sig = capture_jobs_signature(self.REAL_PATH)
        for jid, job in sig["jobs"].items():
            for vf in VOLATILE_JOB_FIELDS:
                self.assertNotIn(
                    vf, job,
                    f"Volatile field {vf} should not be in scoped signature for job {jid}"
                )


if __name__ == "__main__":
    unittest.main()