"""轨道编队共识验收测试。

以子进程方式启动真实 HTTP 服务，覆盖：
- 三节点多数确认提交与序列一致性
- 租约失效后旧主控迟到被栅栏拒绝
- 多数确认后崩溃、重启后已提交结果保留、重传去重与冲突
- 多数不可达时任期申请失败、恢复后追赶
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEASE_MS = "400"


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http(method, port, path, body=None, timeout=5):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


class ServerFixture(unittest.TestCase):
    """每个用例一个独立数据目录与独立服务子进程。"""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="orbit-test-")
        self.proc = None
        self.port = free_port()
        self.start_server()

    def tearDown(self):
        self.stop_server()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def start_server(self, extra_env=None):
        env = dict(os.environ)
        env.update({"PORT": str(self.port), "DATA_DIR": self.data_dir,
                    "LEASE_MS": LEASE_MS, "PYTHONPATH": ROOT})
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.server"],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_healthy()

    def stop_server(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.proc = None

    def wait_healthy(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status, _ = http("GET", self.port, "/health", timeout=1)
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        self.fail("服务未在 %ds 内就绪" % timeout)

    # ---------- 便捷操作 ----------
    def create(self, n=3):
        status, data = http("POST", self.port, "/api/formations", {"nodeCount": n})
        self.assertEqual(status, 200, data)
        return data

    def state(self):
        status, data = http("GET", self.port, "/api/state")
        self.assertEqual(status, 200)
        return data

    def request_term(self, controller):
        return http("POST", self.port, "/api/term/request", {"controllerId": controller})

    def submit(self, controller, term, request_id, command, **extra):
        body = {"controllerId": controller, "term": term,
                "requestId": request_id, "command": command}
        body.update(extra)
        return http("POST", self.port, "/api/commands", body)

    def set_reachable(self, node_id, reachable):
        status, data = http("POST", self.port,
                            "/api/nodes/%s/reachability" % node_id,
                            {"reachable": reachable})
        self.assertEqual(status, 200, data)
        return data

    def wait_lease_expiry(self):
        time.sleep(int(LEASE_MS) / 1000 + 0.4)


class TestHealthAndFormation(ServerFixture):
    def test_health(self):
        status, data = http("GET", self.port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_reject_bad_node_count(self):
        for bad in (2, 6):
            status, data = http("POST", self.port, "/api/formations",
                                {"nodeCount": bad})
            self.assertEqual(status, 400, data)


class TestMajorityCommit(ServerFixture):
    def test_three_node_commit_consistency(self):
        self.create(3)
        status, term = self.request_term("ctrl-A")
        self.assertEqual(status, 200, term)
        self.assertTrue(term["granted"])
        self.assertEqual(term["term"], 1)
        self.assertGreaterEqual(len(term["quorum"]), 2, "需多数确认")

        status, res = self.submit("ctrl-A", 1, "req-1", "ORBIT-RAISE +2.4km")
        self.assertEqual(status, 200, res)
        self.assertEqual(res["status"], "committed")

        snap = self.state()
        # 唯一现役任期
        self.assertEqual(snap["activeTerm"]["term"], 1)
        self.assertEqual(snap["activeTerm"]["controller"], "ctrl-A")
        self.assertTrue(snap["activeTerm"]["leaseActive"])
        self.assertEqual(sorted(snap["activeTerm"]["quorum"]), ["N1", "N2", "N3"])
        # 三节点已提交序列一致
        seqs = [tuple(e["command"] for e in n["log"]) for n in snap["nodes"]]
        self.assertEqual(seqs, [("ORBIT-RAISE +2.4km",)] * 3)
        for node in snap["nodes"]:
            self.assertEqual(node["committedCount"], 1)
        self.assertEqual([e["requestId"] for e in snap["committed"]], ["req-1"])

    def test_five_node_commit_with_minority_unreachable(self):
        self.create(5)
        self.set_reachable("N4", False)
        self.set_reachable("N5", False)
        status, term = self.request_term("ctrl-A")
        self.assertEqual(status, 200, term)
        self.assertEqual(sorted(term["quorum"]), ["N1", "N2", "N3"])
        status, res = self.submit("ctrl-A", 1, "req-1", "PHASE-ADJUST 15deg")
        self.assertEqual(status, 200, res)
        # 恢复后追赶收敛
        snap = self.set_reachable("N4", True)
        snap = self.set_reachable("N5", True)
        for node in snap["nodes"]:
            self.assertEqual([e["command"] for e in node["log"]],
                             ["PHASE-ADJUST 15deg"], node["id"])
            self.assertEqual(node["committedCount"], 1, node["id"])


class TestFencing(ServerFixture):
    """旧主控迟到：租约失效后 B 取得更高任期，A 以旧任期重试必须被栅栏拒绝。"""

    def test_stale_term_fenced_after_lease_expiry(self):
        self.create(3)
        status, term = self.request_term("ctrl-A")
        self.assertEqual(status, 200, term)
        status, res = self.submit("ctrl-A", 1, "req-1", "CMD-FROM-A")
        self.assertEqual(status, 200, res)

        self.wait_lease_expiry()
        status, term_b = self.request_term("ctrl-B")
        self.assertEqual(status, 200, term_b)
        self.assertEqual(term_b["term"], 2)
        status, res = self.submit("ctrl-B", 2, "req-2", "CMD-FROM-B")
        self.assertEqual(status, 200, res)

        # A 以旧任期重试：栅栏拒绝
        status, res = self.submit("ctrl-A", 1, "req-3", "STALE-CMD-FROM-A")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["error"], "fenced")

        # 任一节点日志均不得新增旧指令
        snap = self.state()
        for node in snap["nodes"]:
            commands = [e["command"] for e in node["log"]]
            self.assertNotIn("STALE-CMD-FROM-A", commands, node["id"])
            self.assertEqual(commands, ["CMD-FROM-A", "CMD-FROM-B"], node["id"])
        self.assertEqual(snap["activeTerm"]["term"], 2)

    def test_lease_held_blocks_other_controller(self):
        self.create(3)
        status, _ = self.request_term("ctrl-A")
        self.assertEqual(status, 200)
        # 租约有效期内 B 申请被拒
        status, res = self.request_term("ctrl-B")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["error"], "lease_held")
        # 同一控制器可续租，任期不变
        status, res = self.request_term("ctrl-A")
        self.assertEqual(status, 200, res)
        self.assertEqual(res["term"], 1)
        self.assertTrue(res["renewed"])


class TestCrashAfterMajority(ServerFixture):
    """多数确认后、响应前退出：重启并刷新后仍须显示已提交。"""

    def test_crash_then_restart_keeps_committed(self):
        self.create(3)
        status, _ = self.request_term("ctrl-A")
        self.assertEqual(status, 200)

        # 提交时请求多数确认后退出：连接被切断，响应未返回
        with self.assertRaises(OSError):
            self.submit("ctrl-A", 1, "req-crash", "ORBIT-LOWER -1.2km",
                        crashAfterMajority=True, timeout=5)
        self.proc.wait(timeout=5)
        self.assertIsNotNone(self.proc.returncode, "进程应按模拟崩溃退出")

        # 重启（同一数据目录）并刷新
        self.start_server()
        snap = self.state()
        committed = [e["requestId"] for e in snap["committed"]]
        self.assertIn("req-crash", committed)
        for node in snap["nodes"]:
            self.assertEqual([e["requestId"] for e in node["log"]], ["req-crash"],
                             node["id"])
            self.assertEqual(node["committedCount"], 1, node["id"])

        # 相同请求标识重传：返回原结论且不重复追加
        status, res = self.submit("ctrl-A", 1, "req-crash", "ORBIT-LOWER -1.2km")
        self.assertEqual(status, 200, res)
        self.assertEqual(res["status"], "committed")
        self.assertTrue(res["deduplicated"])
        snap = self.state()
        self.assertEqual(len(snap["committed"]), 1)
        for node in snap["nodes"]:
            self.assertEqual(len(node["log"]), 1, node["id"])

        # 同一标识改换指令内容：冲突
        status, res = self.submit("ctrl-A", 1, "req-crash", "DIFFERENT-CMD")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["error"], "conflict")
        snap = self.state()
        self.assertEqual(len(snap["committed"]), 1)


class TestRetransmitAndConflict(ServerFixture):
    def test_retransmit_dedup_and_conflict(self):
        self.create(3)
        status, _ = self.request_term("ctrl-A")
        self.assertEqual(status, 200)
        status, res = self.submit("ctrl-A", 1, "req-9", "SPIN-UP")
        self.assertEqual(status, 200, res)

        # 重传同标识同内容：原结论、不重复追加
        status, res = self.submit("ctrl-A", 1, "req-9", "SPIN-UP")
        self.assertEqual(status, 200, res)
        self.assertTrue(res["deduplicated"])
        self.assertEqual(res["index"], 1)
        snap = self.state()
        self.assertEqual(len(snap["committed"]), 1)
        for node in snap["nodes"]:
            self.assertEqual(len(node["log"]), 1, node["id"])

        # 同标识换内容：冲突
        status, res = self.submit("ctrl-A", 1, "req-9", "SPIN-DOWN")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["error"], "conflict")
        self.assertEqual(len(self.state()["committed"]), 1)


class TestNoMajorityAndRecovery(ServerFixture):
    def test_no_majority_term_fails_then_recover_catchup_only(self):
        self.create(3)
        status, _ = self.request_term("ctrl-A")
        self.assertEqual(status, 200)
        status, res = self.submit("ctrl-A", 1, "req-1", "CMD-BEFORE-PARTITION")
        self.assertEqual(status, 200, res)

        # 多数节点不可达
        self.set_reachable("N2", False)
        self.set_reachable("N3", False)
        self.wait_lease_expiry()

        # 申请任期失败，现役任期与各节点日志保持不变
        status, res = self.request_term("ctrl-B")
        self.assertEqual(status, 409, res)
        self.assertEqual(res["error"], "no_majority")
        snap = self.state()
        self.assertEqual(snap["activeTerm"]["term"], 1, "现役任期保持不变")
        for node in snap["nodes"]:
            self.assertEqual([e["command"] for e in node["log"]],
                             ["CMD-BEFORE-PARTITION"], node["id"])

        # 旧任期提交：被拒绝（栅栏或无有效租约）且日志不变
        status, res = self.submit("ctrl-A", 1, "req-2", "CMD-DURING-PARTITION")
        self.assertEqual(status, 409, res)
        self.assertIn(res["error"], ("fenced", "no_active_term"))
        for node in self.state()["nodes"]:
            self.assertEqual(len(node["log"]), 1, node["id"])

        # 恢复可达：只能追赶不能提交
        self.set_reachable("N2", True)
        self.set_reachable("N3", True)
        status, res = self.submit("ctrl-A", 1, "req-3", "STALE-AFTER-RECOVERY")
        self.assertEqual(status, 409, res)
        self.assertIn(res["error"], ("fenced", "no_active_term"))
        snap = self.state()
        for node in snap["nodes"]:
            self.assertEqual([e["command"] for e in node["log"]],
                             ["CMD-BEFORE-PARTITION"], node["id"])

        # 新任期可提交，页面刷新读取收敛状态
        status, term = self.request_term("ctrl-B")
        self.assertEqual(status, 200, term)
        status, res = self.submit("ctrl-B", term["term"], "req-4", "CMD-AFTER-RECOVERY")
        self.assertEqual(status, 200, res)
        snap = self.state()
        for node in snap["nodes"]:
            self.assertEqual([e["command"] for e in node["log"]],
                             ["CMD-BEFORE-PARTITION", "CMD-AFTER-RECOVERY"], node["id"])
            self.assertEqual(node["committedCount"], 2, node["id"])


if __name__ == "__main__":
    unittest.main()
