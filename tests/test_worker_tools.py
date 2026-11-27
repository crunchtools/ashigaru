"""The Ashigaru Worker's host tools: the seat registry, the grant editor and
the fence loader's config parsing. Each runs as a subprocess against scratch
files, the way the roles run it on a host."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

FILES = Path(__file__).resolve().parent.parent / "worker" / "roles" / "seat_host" / "files"
POOLS = [
    "--pool=max=10.0.0.61-10.0.0.62",
    "--pool=vertex=10.0.0.41-10.0.0.41",
    "--pool=prime=10.0.0.20-10.0.0.20",
]


class SeatRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.env = {**os.environ, "ASHIGARU_SEATS_SELFTEST": "1", "ASHIGARU_SEATS_DIR": self.dir}

    def seats(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(FILES / "ashigaru-seats"), *args], env=self.env, capture_output=True, text=True
        )

    def allocate(self, name: str, kind: str, *extra: str) -> subprocess.CompletedProcess:
        return self.seats("allocate", "--name", name, "--kind", kind, "--class", kind, *POOLS, *extra)

    def test_a_new_seat_takes_the_lowest_free_slot_and_address(self) -> None:
        first = json.loads(self.allocate("alpha", "max").stdout)
        second = json.loads(self.allocate("beta", "max").stdout)
        self.assertEqual((first["slot"], first["address"]), (1, "10.0.0.61"))
        self.assertEqual((second["slot"], second["address"]), (2, "10.0.0.62"))

    def test_allocate_is_idempotent(self) -> None:
        a = self.allocate("alpha", "max").stdout
        self.assertEqual(self.allocate("alpha", "max").stdout, a)

    def test_a_seat_cannot_change_kind(self) -> None:
        self.allocate("alpha", "max")
        r = self.allocate("alpha", "vertex")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already a max seat", r.stderr)

    def test_an_empty_pool_refuses(self) -> None:
        self.allocate("one", "vertex")
        r = self.allocate("two", "vertex")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no free address", r.stderr)

    def test_prime_is_slot_zero_and_only_one(self) -> None:
        self.assertEqual(json.loads(self.allocate("prime", "prime").stdout)["slot"], 0)
        r = self.allocate("prime2", "prime", "--address", "10.0.0.29")
        self.assertEqual(r.returncode, 1)
        self.assertIn("slot 0 is taken", r.stderr)

    def test_adopted_slots_and_addresses_are_kept_and_skipped(self) -> None:
        self.allocate("old1", "max", "--slot", "1", "--address", "10.0.0.61")
        new = json.loads(self.allocate("new", "max").stdout)
        self.assertEqual((new["slot"], new["address"]), (2, "10.0.0.62"))

    def test_bad_names_are_refused(self) -> None:
        for name in ("Upper", "1digit", "a" * 27, "has space"):
            self.assertEqual(self.allocate(name, "max").returncode, 2, name)

    def test_tsv_lists_workers_not_prime(self) -> None:
        self.allocate("prime", "prime")
        self.allocate("alpha", "max", "--avail", "scott-first")
        out = self.seats("tsv", "--host", "h").stdout
        rows = [r.split("\t") for r in out.splitlines() if not r.startswith("#")]
        want = ["alpha", "h", "max", "max", "1", "10.0.0.61", "alpha", "scott-first", "alpha"]
        self.assertEqual(rows, [want])

    def test_release_frees_the_slot(self) -> None:
        self.allocate("alpha", "max")
        self.seats("release", "--name", "alpha")
        self.assertEqual(json.loads(self.allocate("beta", "max").stdout)["slot"], 1)


class GrantTest(unittest.TestCase):
    YAML = (
        "profiles:\n"
        "  # keep me\n"
        "  seat-max:\n"
        "    client_egress:\n"
        "      source_networks: [10.0.0.61/32]\n"
        "      hosts: [a.example]\n"
        "  other:\n"
        "    client_egress:\n"
        "      source_networks: [10.0.0.9/32]\n"
    )

    def setUp(self) -> None:
        self.file = Path(tempfile.mkdtemp()) / "profiles.yaml"
        self.file.write_text(self.YAML)

    def grant(self, op: str, profile: str, source: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(FILES / "ashigaru-grant"), op, "--file", str(self.file)]
            + ["--profile", profile, "--source", source],
            capture_output=True,
            text=True,
        )

    def test_add_then_remove_restores_the_file_byte_for_byte(self) -> None:
        self.assertEqual(self.grant("add", "seat-max", "10.0.0.62/32").stdout.strip(), "changed")
        self.assertIn("source_networks: [10.0.0.61/32, 10.0.0.62/32]", self.file.read_text())
        self.assertIn("source_networks: [10.0.0.9/32]", self.file.read_text())
        self.assertEqual(self.grant("remove", "seat-max", "10.0.0.62/32").stdout.strip(), "changed")
        self.assertEqual(self.file.read_text(), self.YAML)

    def test_a_second_add_changes_nothing(self) -> None:
        self.grant("add", "seat-max", "10.0.0.62/32")
        self.assertEqual(self.grant("add", "seat-max", "10.0.0.62/32").stdout.strip(), "unchanged")

    def test_the_last_removal_leaves_a_placeholder_the_next_add_drops(self) -> None:
        self.file.write_text(self.YAML.replace("[10.0.0.61/32]", "[0.0.0.1/32]"))
        self.grant("remove", "other", "10.0.0.9/32")
        self.assertIn("source_networks: [0.0.0.2/32]", self.file.read_text())
        self.grant("add", "other", "10.0.0.10/32")
        self.assertIn("source_networks: [10.0.0.10/32]\n", self.file.read_text())

    def test_a_missing_profile_is_an_error(self) -> None:
        self.assertEqual(self.grant("add", "nope", "10.0.0.62/32").returncode, 1)
        self.assertEqual(self.file.read_text(), self.YAML)


class FenceConfigTest(unittest.TestCase):
    GOOD = (
        "SEAT_GATEWAY=10.0.0.55\n"
        "SEAT_SSH_MAX=10.0.0.1 . 22422, 10.0.0.20 . 22\n"
        "SEAT_SSH_VERTEX=10.0.0.1 . 22422, 10.0.0.20 . 22, 10.0.0.10 . 22\n"
        "SEAT_PROBE_IMAGE=quay.io/example/claude:latest\n"
    )

    def ruleset(self, conf: str, cls: str) -> subprocess.CompletedProcess:
        path = Path(tempfile.mkdtemp()) / "seat-fence.conf"
        path.write_text(conf)
        env = {**os.environ, "SEAT_FENCE_SELFTEST": "1", "SEAT_FENCE_CONF": str(path)}
        argv = ["bash", str(FILES / "seat-fence-load"), cls, "--print"]
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    def test_each_billing_gets_its_own_ssh_targets(self) -> None:
        max_rules = self.ruleset(self.GOOD, "max").stdout
        self.assertIn("elements = { 10.0.0.1 . 22422, 10.0.0.20 . 22 }", max_rules)
        self.assertIn("10.0.0.10 . 22 }", self.ruleset(self.GOOD, "vertex").stdout)
        self.assertIn("ip daddr 10.0.0.55 tcp dport { 8019, 8021 } accept", max_rules)

    def test_the_old_class_names_still_work(self) -> None:
        self.assertEqual(self.ruleset(self.GOOD, "dmax").stdout, self.ruleset(self.GOOD, "max").stdout)
        self.assertEqual(self.ruleset(self.GOOD, "bvertex").stdout, self.ruleset(self.GOOD, "vertex").stdout)

    def test_a_malformed_config_is_refused(self) -> None:
        bad = self.GOOD.replace("SEAT_GATEWAY=10.0.0.55", "SEAT_GATEWAY=10.0.0.55; rm -rf /")
        r = self.ruleset(bad, "max")
        self.assertEqual(r.returncode, 2)
        self.assertIn("SEAT_GATEWAY is missing or malformed", r.stderr)
