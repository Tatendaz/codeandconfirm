"""Config, scheduler reservations, result parsers, candidate resolution, redaction."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from codeandconfirm import candidate as cand_mod
from codeandconfirm.config import Config, render, EXAMPLE_CONFIG, REPO_CONFIG_NAME, LOCAL_CONFIG_NAME
from codeandconfirm.results import parse_junit_files, parse_results
from codeandconfirm.scheduler import Reservation, release_all_for, list_locks, auto_functional_slots
from codeandconfirm.util import redact, run, CmdResult


# --- config --------------------------------------------------------------------------

def test_config_layering(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(home))
    (home / "config.toml").write_text('[codex]\nbinary = "/usr/bin/false"\n[scheduler]\nmax_functional_runs = 3\n')
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    (repo / REPO_CONFIG_NAME).write_text(EXAMPLE_CONFIG)
    (repo / LOCAL_CONFIG_NAME).write_text('[codex]\nreasoning_effort = "low"\n[profiles.pr.codex]\nreasoning_effort = "minimal"\n')
    cfg = Config.load(repo_root=repo)
    assert cfg.get("codex.binary") == "/usr/bin/false"
    assert cfg.get("scheduler.max_functional_runs") == 3
    # the example config selects profile `pr` by default; profiles merge over the layered result, and the
    # local file can still override a profile's values through its own [profiles.pr.*] table
    assert cfg.get("profile.name") == "pr"
    assert cfg.get("codex.reasoning_effort") == "minimal"
    assert Config.load(repo_root=repo, profile="full").get("codex.reasoning_effort") == "high"
    assert cfg.get("codex.model") == "gpt-6-astra"
    assert set(cfg.platforms) == {"ios", "android"}
    assert len(cfg.sources) == 3


def test_example_config_has_no_personal_paths():
    for needle in ("/Users/", "/home/", "@gmail", "@me.com"):
        assert needle not in EXAMPLE_CONFIG


def test_render_placeholders():
    assert render("x {run_dir}/y {auth_port}", {"run_dir": "/r", "auth_port": 9}) == "x /r/y 9"
    with pytest.raises(KeyError):
        render("{nope}", {})
    assert render("{nope}", {}, strict=False) == "{nope}"


# --- reservations -----------------------------------------------------------------------

def test_reservation_conflict_and_stale_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path))
    a = Reservation("ios-device:X", "run-a", ttl_s=60)
    assert a.try_acquire()
    b = Reservation("ios-device:X", "run-b", ttl_s=60)
    assert not b.try_acquire()                       # held by a live owner
    # simulate a crashed owner: dead pid
    info = a.current(); info.owner_pid = 999999
    from codeandconfirm.util import write_json
    write_json(a.path, info.__dict__)
    assert b.try_acquire()                           # stale lock reclaimed
    assert b.current().owner_run == "run-b"
    b.release()
    assert b.current() is None


def test_release_all_for(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEANDCONFIRM_HOME", str(tmp_path))
    Reservation("r1", "run-z", 60).try_acquire(); Reservation("r2", "run-z", 60).try_acquire(); Reservation("r3", "other", 60).try_acquire()
    assert sorted(release_all_for("run-z")) == ["r1", "r2"]
    assert [l.resource for l in list_locks()] == ["r3"]


def test_auto_slots():
    assert auto_functional_slots(16) == 1
    assert auto_functional_slots(48) == 2
    assert auto_functional_slots(128) == 5


# --- results ------------------------------------------------------------------------------

def test_junit_parse(tmp_path):
    x = tmp_path / "TEST-a.xml"
    x.write_text('<testsuite name="s" tests="3"><testcase classname="C" name="ok"/><testcase classname="C" name="bad"><failure message="x"/></testcase>'
                 '<testcase classname="C" name="skip"><skipped/></testcase></testsuite>')
    r = parse_junit_files([x])
    assert (r.total, r.passed, r.failed, r.skipped) == (3, 1, 1, 1)
    assert r.failed_tests == ["C.bad"] and not r.green


def test_junit_node_reporter_shape(tmp_path):
    x = tmp_path / "node.xml"
    x.write_text('<?xml version="1.0"?><testsuites><testcase name="a" classname="t"/><testcase name="b" classname="t"><failure/></testcase></testsuites>')
    r = parse_junit_files([x])
    assert (r.total, r.passed, r.failed) == (2, 1, 1) and r.parsed


def test_junit_refuses_dtd(tmp_path):
    x = tmp_path / "evil.xml"
    x.write_text('<!DOCTYPE t [<!ENTITY e "x">]><testsuite><testcase name="&e;"/></testsuite>')
    r = parse_junit_files([x])
    assert not r.parsed


def test_missing_xcresult_not_parsed(tmp_path):
    r = parse_results("xcresult", tmp_path / "nope.xcresult")
    assert not r.parsed and not r.green


# --- candidate ----------------------------------------------------------------------------

def _git(repo: Path, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feat")
    (repo / "a.txt").write_text("2\n"); (repo / "b.txt").write_text("x\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "feat")
    return repo


def test_resolve_branch_and_worktree(tmp_path):
    repo = make_repo(tmp_path)
    c = cand_mod.resolve_branch(repo, "feat", "main")
    assert c.source == "branch" and sorted(c.changed_files) == ["a.txt", "b.txt"]
    assert c.base_sha == c.merge_base_sha != c.candidate_sha
    wt = cand_mod.create_worktree(repo, c.candidate_sha, tmp_path / "wt")
    assert cand_mod.head_sha(wt) == c.candidate_sha
    assert cand_mod.worktree_is_pristine(wt) == (True, [])
    (wt / "a.txt").write_text("tampered\n")
    ok, offending = cand_mod.worktree_is_pristine(wt)
    assert not ok and offending == ["a.txt"]
    # the developer's checkout is untouched
    assert (repo / "a.txt").read_text() == "2\n"
    cand_mod.remove_worktree(repo, wt)
    assert not wt.exists()


def test_uncommitted_changes_are_flagged(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("dirty\n")
    c = cand_mod.resolve_branch(repo, "feat", "main")
    assert "uncommitted" in c.acceptance_criteria


def test_parse_pr_specs():
    assert cand_mod.parse_pr("https://github.com/o/r/pull/12") == ("o/r", 12)
    assert cand_mod.parse_pr("o/r#7") == ("o/r", 7)
    assert cand_mod.parse_pr("9", "o/r") == ("o/r", 9)
    with pytest.raises(ValueError):
        cand_mod.parse_pr("9")


# --- util ------------------------------------------------------------------------------------

def test_redaction():
    s = "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and Bearer abcdefghijklmnopqrstuvwxyz123 and sk-abcdefghijklmnopqrstuv"
    r = redact(s)
    assert "ghp_" not in r and "abcdefghijklmnopqrstuvwxyz123" not in r and "sk-abc" not in r


def test_run_preserves_exit_code_and_timeout():
    r = run("exit 7")
    assert r.exit_code == 7 and not r.ok
    r = run("sleep 5", timeout=0.5)
    assert r.timed_out and not r.ok
    r = run("false | true")   # pipefail-free shell: exit 0; the coordinator records real per-suite codes separately
    assert isinstance(r, CmdResult)


# --- suites -----------------------------------------------------------------------------

def test_exit_code_only_suite_is_green_without_a_retry(tmp_path):
    from codeandconfirm.target import run_suite
    out = run_suite("logic", {"command": "echo ok", "results": {"kind": "none"}}, checkout=tmp_path, values={}, artifacts_dir=tmp_path,
                    timeout_s=30, record_to=tmp_path / "commands.jsonl", retries=1)
    assert out.green and out.attempts == 1 and out.flaky_history == []
    bad = run_suite("logic", {"command": "exit 3", "results": {"kind": "none"}}, checkout=tmp_path, values={}, artifacts_dir=tmp_path,
                    timeout_s=30, record_to=tmp_path / "commands.jsonl", retries=1)
    assert not bad.green and bad.exit_code == 3
