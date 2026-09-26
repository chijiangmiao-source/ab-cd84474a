"""HTTP 冒烟：对运行中的服务验证健康路径、任期申请、指令提交。

环境变量：
  APP_URL          服务地址（默认 http://localhost:8080）
  HOST_HEALTH_URL  可选，宿主机端口映射出的健康路径（验证可配置宿主机端口）
  SMOKE_CRASH_TEST 为 1 时执行端到端崩溃恢复冒烟（依赖容器重启策略）
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

APP_URL = os.environ.get("APP_URL", "http://localhost:8080").rstrip("/")
HOST_HEALTH_URL = os.environ.get("HOST_HEALTH_URL", "").strip()
SMOKE_CRASH_TEST = os.environ.get("SMOKE_CRASH_TEST") == "1"

failures = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print("[%s] %s%s" % (mark, name, (" — " + str(detail)) if detail else ""), flush=True)
    if not cond:
        failures.append(name)


def http(method, url, body=None, timeout=5):
    data = headers = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def wait_health(url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, data = http("GET", url + "/health", timeout=2)
            if status == 200 and data.get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def main():
    print("== HTTP 冒烟 目标: %s ==" % APP_URL, flush=True)

    # 健康路径
    check("健康路径 /health", wait_health(APP_URL),
          "GET %s/health" % APP_URL)
    if HOST_HEALTH_URL:
        base = HOST_HEALTH_URL.rstrip("/")
        try:
            status, data = http("GET", base, timeout=5)
            check("宿主机端口健康路径", status == 200 and data.get("status") == "ok",
                  "%s -> %s" % (base, status))
        except OSError as exc:
            check("宿主机端口健康路径", False, exc)

    # 创建三节点编队
    status, data = http("POST", APP_URL + "/api/formations", {"nodeCount": 3})
    check("创建三节点编队", status == 200 and data.get("initialized"), data.get("error"))

    # 任期申请
    status, term = http("POST", APP_URL + "/api/term/request",
                        {"controllerId": "smoke-ctrl"})
    check("任期申请获得多数确认",
          status == 200 and term.get("granted") and len(term.get("quorum", [])) >= 2,
          term)
    term_no = term.get("term", 1)

    # 指令提交
    status, res = http("POST", APP_URL + "/api/commands", {
        "controllerId": "smoke-ctrl", "term": term_no,
        "requestId": "smoke-req-1", "command": "SMOKE-CMD-1"})
    check("指令提交获得多数确认", status == 200 and res.get("status") == "committed", res)

    # 状态快照：唯一现役任期 + 已提交序列
    status, snap = http("GET", APP_URL + "/api/state")
    active = (snap.get("activeTerm") or {})
    check("状态展示唯一现役任期",
          status == 200 and active.get("term") == term_no
          and active.get("controller") == "smoke-ctrl", active)
    check("三节点已提交序列一致",
          len({tuple(e["requestId"] for e in n["log"]) for n in snap.get("nodes", [])}) == 1
          and any(e["requestId"] == "smoke-req-1" for e in snap.get("committed", [])),
          [n["id"] for n in snap.get("nodes", [])])

    # 重传去重与冲突
    status, res = http("POST", APP_URL + "/api/commands", {
        "controllerId": "smoke-ctrl", "term": term_no,
        "requestId": "smoke-req-1", "command": "SMOKE-CMD-1"})
    check("相同请求标识重传返回原结论",
          status == 200 and res.get("deduplicated"), res)
    status, res = http("POST", APP_URL + "/api/commands", {
        "controllerId": "smoke-ctrl", "term": term_no,
        "requestId": "smoke-req-1", "command": "SMOKE-CMD-OTHER"})
    check("同一标识改换内容返回冲突", status == 409 and res.get("error") == "conflict", res)

    # 端到端崩溃恢复（依赖容器重启策略）
    if SMOKE_CRASH_TEST:
        try:
            http("POST", APP_URL + "/api/commands", {
                "controllerId": "smoke-ctrl", "term": term_no,
                "requestId": "smoke-req-crash", "command": "SMOKE-CMD-CRASH",
                "crashAfterMajority": True}, timeout=5)
            check("多数确认后退出（连接应中断）", False, "提交意外成功返回")
        except OSError:
            check("多数确认后退出（连接应中断）", True)
        check("崩溃后自动重启并恢复健康", wait_health(APP_URL, timeout=60))
        status, snap = http("GET", APP_URL + "/api/state")
        check("重启刷新后仍显示已提交",
              status == 200
              and any(e["requestId"] == "smoke-req-crash"
                      for e in snap.get("committed", [])), status)
        status, res = http("POST", APP_URL + "/api/commands", {
            "controllerId": "smoke-ctrl", "term": term_no,
            "requestId": "smoke-req-crash", "command": "SMOKE-CMD-CRASH"})
        check("崩溃指令重传去重不追加",
              status == 200 and res.get("deduplicated")
              and len(snap.get("committed", [])) == 2, res)

    print("== 冒烟结果: %s ==" % ("全部通过" if not failures else "失败 %d 项" % len(failures)),
          flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
