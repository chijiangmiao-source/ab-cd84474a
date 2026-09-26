"""轨道编队共识核心。

在单个进程内模拟 3-5 个控制节点的多数派共识：

- 任期申请：控制器以标识申请任期，需多数可达节点确认，授予后持有租约；
- 指令提交：仅现役任期持有者可提交，复制到达多数节点后提交并持久化；
- 栅栏：携带旧任期的请求一律被拒绝，节点日志不得追加旧指令；
- 幂等：稳定请求标识去重，重传返回原结论，同标识换内容返回冲突；
- 追赶：不可达节点恢复后同步已提交序列与最高任期；
- 持久化：全部状态写入数据目录，进程重启后已提交结果不丢失。
"""

import json
import os
import threading
import time


def now_ms():
    return int(time.time() * 1000)


class ClusterError(Exception):
    """业务错误，携带 HTTP 状态码与机器可读错误码。"""

    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


class Cluster:
    def __init__(self, data_dir, lease_ms=4000, crash_after_majority=False):
        self.data_dir = data_dir
        self.lease_ms = lease_ms
        # 环境级崩溃开关：开启后首个提交在多数确认、响应前令进程退出
        self.crash_after_majority = crash_after_majority
        self._lock = threading.RLock()
        self._state_file = os.path.join(data_dir, "cluster.json")
        self._state = self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self._state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._state_file)

    def _require(self):
        if self._state is None:
            raise ClusterError(409, "not_initialized", "尚未创建编队，请先创建 3-5 节点编队")
        return self._state

    # ------------------------------------------------------------------
    # 编队管理
    # ------------------------------------------------------------------
    def create_formation(self, node_count, node_ids=None):
        with self._lock:
            if not isinstance(node_count, int) or not 3 <= node_count <= 5:
                raise ClusterError(400, "bad_node_count", "节点数量须为 3 至 5")
            ids = node_ids or ["N%d" % (i + 1) for i in range(node_count)]
            if len(ids) != node_count or len(set(ids)) != node_count:
                raise ClusterError(400, "bad_node_ids", "节点标识数量不符或存在重复")
            self._state = {
                "order": list(ids),
                "nodes": {
                    nid: {"currentTerm": 0, "votedFor": None, "log": [], "reachable": True}
                    for nid in ids
                },
                "committed": [],
                "requests": {},
                "activeTerm": None,
                "maxTerm": 0,
            }
            self._save()
            return self.snapshot()

    def set_reachable(self, node_id, reachable):
        with self._lock:
            state = self._require()
            node = state["nodes"].get(node_id)
            if node is None:
                raise ClusterError(404, "no_such_node", "节点不存在: %s" % node_id)
            node["reachable"] = bool(reachable)
            if reachable:
                self._catch_up(state, node)
            self._save()
            return self.snapshot()

    def _catch_up(self, state, node):
        """追赶：恢复可达的节点同步已提交序列与最高任期，只能追赶不能提交。"""
        node["log"] = [dict(entry) for entry in state["committed"]]
        node["committedCount"] = len(state["committed"])
        node["currentTerm"] = max(node["currentTerm"], state["maxTerm"])

    # ------------------------------------------------------------------
    # 任期申请
    # ------------------------------------------------------------------
    def request_term(self, controller_id):
        with self._lock:
            state = self._require()
            if not controller_id:
                raise ClusterError(400, "bad_controller", "控制器标识不能为空")
            now = now_ms()
            active = state["activeTerm"]
            if active and active["leaseExpiry"] > now:
                if active["controller"] != controller_id:
                    raise ClusterError(
                        409, "lease_held",
                        "现役租约由 %s 持有（任期 %d），租约失效后方可申请"
                        % (active["controller"], active["term"]),
                        {"activeTerm": active["term"], "controller": active["controller"]})
                # 同一控制器续租：任期不变，租约顺延
                active["leaseExpiry"] = now + self.lease_ms
                self._save()
                return {"granted": True, "term": active["term"], "renewed": True,
                        "quorum": list(active["quorum"]), "leaseExpiry": active["leaseExpiry"]}

            new_term = state["maxTerm"] + 1
            voters = []
            for nid in state["order"]:
                node = state["nodes"][nid]
                if not node["reachable"]:
                    continue
                if new_term > node["currentTerm"]:
                    node["currentTerm"] = new_term
                    node["votedFor"] = controller_id
                    voters.append(nid)
            state["maxTerm"] = max(state["maxTerm"],
                                   max((state["nodes"][n]["currentTerm"] for n in state["order"]),
                                       default=state["maxTerm"]))
            if len(voters) <= len(state["order"]) // 2:
                # 多数不可达：现役任期与各节点日志保持不变
                self._save()
                raise ClusterError(
                    409, "no_majority",
                    "多数节点不可达，任期申请失败（%d/%d 确认）"
                    % (len(voters), len(state["order"])),
                    {"voters": voters})
            state["activeTerm"] = {
                "term": new_term,
                "controller": controller_id,
                "leaseExpiry": now + self.lease_ms,
                "quorum": voters,
            }
            self._save()
            return {"granted": True, "term": new_term, "renewed": False,
                    "quorum": voters, "leaseExpiry": state["activeTerm"]["leaseExpiry"]}

    # ------------------------------------------------------------------
    # 指令提交
    # ------------------------------------------------------------------
    def submit_command(self, controller_id, term, request_id, command,
                       crash_after_majority=False):
        with self._lock:
            state = self._require()
            if not request_id:
                raise ClusterError(400, "bad_request_id", "请求标识不能为空")
            if not command:
                raise ClusterError(400, "bad_command", "指令内容不能为空")
            if not isinstance(term, int) or term < 1:
                raise ClusterError(400, "bad_term", "任期必须为正整数")

            # 幂等：稳定请求标识优先于一切后续校验
            record = state["requests"].get(request_id)
            if record is not None:
                if record["command"] == command:
                    return {"status": record["status"], "deduplicated": True,
                            "requestId": request_id, "term": record["term"],
                            "index": record["index"], "quorum": list(record["quorum"])}
                raise ClusterError(
                    409, "conflict",
                    "请求标识 %s 已绑定其他指令内容，拒绝变更" % request_id,
                    {"existing": record})

            # 栅栏：旧任期一律拒绝，任何节点日志不得新增旧指令
            active = state["activeTerm"]
            if term < state["maxTerm"] or (active and term < active["term"]):
                raise ClusterError(
                    409, "fenced",
                    "任期 %d 已被栅栏隔离（当前最高任期 %d）" % (term, state["maxTerm"]),
                    {"maxTerm": state["maxTerm"]})
            lease_active = active is not None and active["leaseExpiry"] > now_ms()
            if not lease_active:
                raise ClusterError(409, "no_active_term", "无有效租约，请先申请任期")
            if term != active["term"] or controller_id != active["controller"]:
                raise ClusterError(
                    409, "not_leader",
                    "控制器 %s 并非现役任期 %d 的持有者"
                    % (controller_id, active["term"]))

            # 复制到全部可达节点（节点级栅栏兜底）
            index = len(state["committed"]) + 1
            entry = {"index": index, "term": term, "requestId": request_id,
                     "command": command}
            accepted = []
            for nid in state["order"]:
                node = state["nodes"][nid]
                if not node["reachable"] or term < node["currentTerm"]:
                    continue
                node["log"].append(dict(entry))
                accepted.append(nid)

            if len(accepted) <= len(state["order"]) // 2:
                # 未达多数：回滚已复制副本，各节点日志保持不变
                for nid in accepted:
                    node = state["nodes"][nid]
                    node["log"] = [e for e in node["log"] if e["index"] != index]
                self._save()
                raise ClusterError(
                    503, "no_quorum",
                    "未获得多数确认（%d/%d），指令未提交"
                    % (len(accepted), len(state["order"])),
                    {"accepted": accepted})

            # 多数确认：提交并持久化，随后才响应
            entry["quorum"] = accepted
            state["committed"].append(entry)
            for nid in accepted:
                state["nodes"][nid]["committedCount"] = len(state["committed"])
            state["requests"][request_id] = {
                "command": command, "term": term, "index": index,
                "status": "committed", "quorum": list(accepted),
            }
            self._save()

            if crash_after_majority or self.crash_after_majority:
                # 多数确认后、响应前触发退出；已提交结果已持久化
                self._save()
                os._exit(1)

            return {"status": "committed", "deduplicated": False,
                    "requestId": request_id, "term": term,
                    "index": index, "quorum": accepted}

    # ------------------------------------------------------------------
    # 状态快照
    # ------------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            if self._state is None:
                return {"initialized": False, "leaseMs": self.lease_ms}
            state = self._state
            # 页面刷新可读取收敛状态：可达且落后的节点即时追赶
            converged = False
            for nid in state["order"]:
                node = state["nodes"][nid]
                if node["reachable"] and node.get("committedCount", 0) < len(state["committed"]):
                    self._catch_up(state, node)
                    converged = True
            if converged:
                self._save()

            now = now_ms()
            active = None
            if state["activeTerm"] is not None:
                active = dict(state["activeTerm"])
                active["quorum"] = list(active["quorum"])
                active["leaseRemainingMs"] = active["leaseExpiry"] - now
                active["leaseActive"] = active["leaseRemainingMs"] > 0
            nodes = []
            for nid in state["order"]:
                node = state["nodes"][nid]
                nodes.append({
                    "id": nid,
                    "reachable": node["reachable"],
                    "currentTerm": node["currentTerm"],
                    "votedFor": node["votedFor"],
                    "committedCount": node.get("committedCount", 0),
                    "log": [dict(e) for e in node["log"]],
                })
            return {
                "initialized": True,
                "leaseMs": self.lease_ms,
                "nowMs": now,
                "maxTerm": state["maxTerm"],
                "activeTerm": active,
                "committed": [dict(e) for e in state["committed"]],
                "nodes": nodes,
                "requestCount": len(state["requests"]),
            }
