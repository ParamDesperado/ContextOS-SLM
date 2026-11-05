"""Unit tests. They need no model: a scripted backend stands in for the SLM."""
import json
import subprocess
import sys
import time

import psutil
import pytest

from contextos import config
from contextos.context.activity import classify
from contextos.context.sensors import Sampler
from contextos.context.store import ContextStore
from contextos.context.summary import summarize
from contextos.core import ContextOS
from contextos.governor import Governor
from contextos.security.audit import AuditLog
from contextos.security.policy import ConfirmationQueue, Decision, Risk, evaluate, is_protected
from contextos.slm.backends import ScriptedBackend
from contextos.slm.planner import RulePlanner, SLMPlanner, balanced_json, validate
from contextos.slm.service import BACKGROUND, AdmissionDenied, InferenceService
from contextos.tools import PRIORITY, PriorityLedger, Toolbox, find_processes


@pytest.fixture
def snap():
    return Sampler().sample().to_dict()


@pytest.fixture
def toolbox(snap, tmp_path):
    return Toolbox(lambda: snap, search_root=tmp_path)


def make_system(tmp_path, reply):
    return ContextOS(ScriptedBackend(reply), 0.0, home=tmp_path, search_root=tmp_path, start_sampler=False)


# ----- context engine
def test_sampler_produces_sane_values(snap):
    assert 0 <= snap["cpu_percent"] <= 100
    assert snap["mem_total_gb"] > 0
    assert isinstance(snap["top_mem"], list) and snap["activity"]


def test_activity_rules():
    assert classify("Code.exe") == "coding"
    assert classify("chrome.exe") == "browsing"
    assert classify("chrome.exe", idle_seconds=999) == "idle"
    assert classify(None) == "unknown"
    assert classify("mystery.exe") == "other"


def test_summary_is_short_and_has_no_exe_suffix(snap):
    snap["top_mem"] = [{"pid": 1, "name": "chrome.exe", "cpu": 3, "mem_mb": 3000}]
    text = summarize(snap)
    assert "chrome 3000MB" in text and ".exe" not in text and len(text) < 500


def test_store_round_trip(tmp_path):
    store = ContextStore(tmp_path / "c.db")
    sampler = Sampler()
    store.add(sampler.sample())
    assert len(store.history(60)) == 1
    assert sum(store.activity_minutes(60).values()) == 1
    store.close()


# ----- planner and validation of untrusted output
def test_balanced_json_handles_nesting_strings_and_trailing_text():
    assert balanced_json('x {"a": {"b": "}"}} trailing') == '{"a": {"b": "}"}}'
    assert balanced_json('{"a": 1') is None


def test_validate_rejects_bad_output(toolbox):
    tools = toolbox.tools
    assert validate({"tool": "system_status"}, tools) == ("system_status", {})
    for bad in ({"tool": "format_disk"}, {"tool": "terminate_process"}, ["x"], {"tool": "find_files", "args": "x"}):
        with pytest.raises(ValueError):
            validate(bad, tools)


def test_validate_drops_unknown_args_and_bad_enums(toolbox):
    name, args = validate({"tool": "top_processes", "args": {"sort_by": "colour", "evil": "1"}}, toolbox.tools)
    assert name == "top_processes" and args == {}


def test_slm_planner_parses_seeded_output(toolbox):
    service = InferenceService(ScriptedBackend('{"tool": "disk_usage", "args": {}} extra'), 0.0)
    plan = SLMPlanner(service, toolbox.tools).plan("how much space is left")
    service.stop()
    assert plan.tool == "disk_usage" and not plan.error


@pytest.mark.parametrize("text,tool", [
    ("kill chrome", "terminate_process"), ("why is my laptop so slow", "explain_slowdown"),
    ("how much disk space is left", "disk_usage"), ("find my resume", "find_files"),
    ("turn on focus mode", "focus_mode"), ("what is the capital of france", "answer"),
])
def test_rule_planner(text, tool):
    assert RulePlanner().plan(text).tool == tool


def test_rule_planner_cannot_resolve_references():
    assert RulePlanner().plan("kill it").tool != "terminate_process"


# ----- security
def test_protected_processes():
    assert is_protected("csrss.exe") and is_protected("SYSTEMD") and not is_protected("chrome.exe")


def test_policy_decisions(toolbox):
    tools = toolbox.tools
    assert evaluate(tools["system_status"], {}).decision is Decision.ALLOW
    assert evaluate(tools["set_priority"], {"process": "chrome"}).decision is Decision.ALLOW
    assert evaluate(tools["set_priority"], {"process": "lsass.exe"}).decision is Decision.DENY
    assert evaluate(tools["terminate_process"], {"process": "chrome"}).decision is Decision.CONFIRM
    assert evaluate(tools["terminate_process"], {"process": "svchost"}).decision is Decision.DENY
    assert tools["terminate_process"].risk is Risk.DANGEROUS


def test_confirmation_is_one_time_and_expires():
    queue = ConfirmationQueue()
    token = queue.create("terminate_process", {"process": "x"})
    assert queue.consume(token) == ("terminate_process", {"process": "x"})
    assert queue.consume(token) is None
    queue.TTL_S = -1
    assert queue.consume(queue.create("t", {})) is None


def test_audit_detects_tampering(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path)
    for index in range(3):
        log.append("slm", "tool", {"i": index}, "allow")
    assert log.verify() == (True, 3)
    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["decision"] = "deny"
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    assert AuditLog(path).verify() == (False, 1)


def test_audit_chain_survives_reopen(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path).append("a", "b", {}, "allow")
    AuditLog(path).append("a", "c", {}, "allow")
    assert AuditLog(path).verify() == (True, 2)


# ----- inference service
def test_service_lazy_load_and_idle_unload():
    backend = ScriptedBackend("hi")
    service = InferenceService(backend, 0.0, idle_unload_s=0.1)
    assert not backend.loaded()
    assert service.generate(system="s", user="u").text == "hi"
    time.sleep(2.5)
    assert not backend.loaded() and service.metrics.unloads == 1
    service.stop()


def test_service_admission_control():
    service = InferenceService(ScriptedBackend("hi"), min_free_ram_gb=10_000)
    with pytest.raises(AdmissionDenied):
        service.generate(system="s", user="u")
    assert service.metrics.denied == 1
    service.stop()


def test_service_interactive_overtakes_background():
    order = []

    class Slow(ScriptedBackend):
        def generate(self, system, user, **kwargs):
            order.append(user)
            time.sleep(0.05)
            return super().generate(system, user, **kwargs)

    service = InferenceService(Slow("x"), 0.0)
    first = service.submit(system="s", user="running")
    time.sleep(0.01)
    low = service.submit(BACKGROUND, system="s", user="background")
    high = service.submit(system="s", user="interactive")
    for future in (first, low, high):
        future.result(5)
    service.stop()
    assert order.index("interactive") < order.index("background")


# ----- end-to-end flow
def test_ask_confirms_dangerous_action_and_never_runs_it_directly(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    name = psutil.Process(child.pid).name()
    reply = json.dumps({"tool": "terminate_process", "args": {"process": "no_such_process_xyz"}})
    system = make_system(tmp_path, reply)
    response = system.ask("kill it")
    assert response["decision"] == "confirm" and response["token"]
    assert child.poll() is None and name
    outcome = system.confirm(response["token"])
    assert "No running process" in outcome["text"]
    assert "expired" in system.confirm(response["token"])["text"]
    system.shutdown()
    child.kill()


def test_ask_denies_protected_process(tmp_path):
    system = make_system(tmp_path, json.dumps({"tool": "terminate_process", "args": {"process": "lsass.exe"}}))
    response = system.ask("kill lsass")
    assert response["decision"] == "deny" and "Refused" in response["text"]
    assert system.audit.read()[-1]["decision"] == "deny"
    system.shutdown()


def test_garbage_from_model_falls_back_to_rules(tmp_path):
    system = make_system(tmp_path, "sorry, I cannot produce JSON")
    response = system.ask("how much disk space is left")
    assert response["planner"] == "rules" and response["tool"] == "disk_usage"
    assert response["planner_note"]
    system.shutdown()


def test_hallucinated_tool_cannot_execute(tmp_path):
    system = make_system(tmp_path, '{"tool": "delete_all_files", "args": {}}')
    response = system.ask("what is the meaning of life")
    assert response["tool"] == "answer"
    system.shutdown()


def test_find_files_stays_in_root_and_skips_hidden(toolbox, tmp_path):
    (tmp_path / "report.txt").write_text("x")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "report_key").write_text("x")
    result = toolbox.find_files("report")
    assert len(result["data"]["hits"]) == 1 and result["data"]["hits"][0].endswith("report.txt")


# ----- real scheduling effect and the governor
def test_priority_change_and_restore_on_a_real_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proc = psutil.Process(child.pid)
        original = proc.nice()
        ledger = PriorityLedger()
        ledger.demote(proc, PRIORITY["below_normal"])
        assert proc.nice() == PRIORITY["below_normal"]
        ledger.restore_all()
        # An unprivileged Linux process may lower a nice value's priority but not raise it back.
        if sys.platform == "win32" or psutil.Process().uids().effective == 0:
            assert proc.nice() == original
    finally:
        child.kill()


def _fake_snap(cpu, procs=(), activity="coding"):
    return {"cpu_percent": cpu, "activity": activity, "foreground": "Code.exe", "top_cpu": list(procs)}


def test_governor_hysteresis_and_protection():
    demoted = []
    ledger = PriorityLedger()
    gov = Governor(ledger, demote=demoted.append)
    hog = {"pid": 4242, "name": "hog.exe", "cpu": 300.0, "mem_mb": 1}
    fg = {"pid": 7, "name": "Code.exe", "cpu": 90.0, "mem_mb": 1}
    sys_proc = {"pid": 8, "name": "svchost.exe", "cpu": 90.0, "mem_mb": 1}
    trio = (hog, fg, sys_proc)
    assert gov.step(_fake_snap(90, trio)) is None          # one high sample is not enough
    event = gov.step(_fake_snap(90, trio))
    assert event["event"] == "demote" and demoted == [4242]  # never foreground, never protected
    assert gov.engaged
    assert gov.step(_fake_snap(60)) is None                 # between thresholds: hold
    for _ in range(config.GOV_RELEASE_SAMPLES - 1):
        assert gov.step(_fake_snap(20)) is None
    assert gov.step(_fake_snap(20))["event"] == "restore"
    assert not gov.engaged


def test_governor_stays_out_when_user_is_idle():
    demoted = []
    gov = Governor(PriorityLedger(), demote=demoted.append)
    hog = {"pid": 4242, "name": "hog.exe", "cpu": 300.0, "mem_mb": 1}
    for _ in range(5):
        gov.step(_fake_snap(95, (hog,), activity="idle"))
    assert demoted == []


def test_find_processes_matches_case_insensitively():
    me = psutil.Process().name()
    assert find_processes(me.upper())


# ----- dashboard
def test_web_api(tmp_path):
    from contextos.web import create_app
    system = make_system(tmp_path, '{"tool": "system_status", "args": {}}')
    app = create_app(system)
    app.testing = True
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert "snapshot" in client.get("/api/state").get_json()
    assert client.post("/api/ask", json={"text": "status"}).get_json()["tool"] == "system_status"
    assert client.post("/api/ask", data="text=x").status_code == 415
    assert client.get("/api/audit/verify").get_json()["intact"]
    system.shutdown()


def test_web_rejects_foreign_host_and_origin(tmp_path):
    from contextos.web import create_app
    system = make_system(tmp_path, '{"tool": "system_status", "args": {}}')
    client = create_app(system).test_client()
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/ask", json={"text": "x"}, headers={"Host": f"127.0.0.1:{config.PORT}",
                       "Origin": "https://evil.example"}).status_code == 403
    system.shutdown()
