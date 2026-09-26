"""轨道编队控制台 HTTP 服务。

路由：
  GET  /                              控制台页面
  GET  /health | /healthz             健康路径
  GET  /api/state                     集群状态快照（现役任期/确认节点/各节点序列）
  POST /api/formations                创建编队 {"nodeCount": 3..5}
  POST /api/nodes/<id>/reachability   设置节点可达性 {"reachable": true|false}
  POST /api/term/request              申请任期 {"controllerId": "..."}
  POST /api/commands                  提交指令 {"controllerId","term","requestId","command",
                                               "crashAfterMajority"?}
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cluster import Cluster, ClusterError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def make_handler(cluster):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OrbitFormation/1.0"

        # ---------------- 工具 ----------------
        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, err):
            self._send_json(err.status, {
                "error": err.code, "message": err.message, "detail": err.detail,
            })

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ClusterError(400, "bad_json", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ClusterError(400, "bad_json", "请求体须为 JSON 对象")
            return data

        def log_message(self, fmt, *args):  # 静默访问日志
            pass

        # ---------------- GET ----------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/health", "/healthz"):
                    self._send_json(200, {"status": "ok"})
                elif path == "/api/state":
                    self._send_json(200, cluster.snapshot())
                elif path == "/" or path == "/index.html":
                    self._serve_index()
                else:
                    self._send_json(404, {"error": "not_found", "message": "路径不存在"})
            except ClusterError as err:
                self._send_error(err)

        def _serve_index(self):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------------- POST ----------------
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
                if path == "/api/formations":
                    result = cluster.create_formation(body.get("nodeCount"),
                                                      body.get("nodeIds"))
                    self._send_json(200, result)
                elif path == "/api/term/request":
                    result = cluster.request_term(body.get("controllerId"))
                    self._send_json(200, result)
                elif path == "/api/commands":
                    result = cluster.submit_command(
                        body.get("controllerId"),
                        body.get("term"),
                        body.get("requestId"),
                        body.get("command"),
                        crash_after_majority=bool(body.get("crashAfterMajority")))
                    self._send_json(200, result)
                else:
                    match = re.fullmatch(r"/api/nodes/([^/]+)/reachability", path)
                    if match:
                        result = cluster.set_reachable(match.group(1),
                                                       bool(body.get("reachable")))
                        self._send_json(200, result)
                    else:
                        self._send_json(404, {"error": "not_found", "message": "路径不存在"})
            except ClusterError as err:
                self._send_error(err)

    return Handler


def create_server(port=None, data_dir=None, lease_ms=None, crash_after_majority=None):
    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    data_dir = data_dir or os.environ.get("DATA_DIR", "./data")
    lease_ms = int(lease_ms if lease_ms is not None else os.environ.get("LEASE_MS", "4000"))
    if crash_after_majority is None:
        crash_after_majority = os.environ.get("CRASH_AFTER_MAJORITY") == "1"
    cluster = Cluster(data_dir, lease_ms=lease_ms, crash_after_majority=crash_after_majority)
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(cluster))


def main():
    server = create_server()
    port = server.server_address[1]
    print("轨道编队控制台已启动: http://0.0.0.0:%d (健康路径 /health)" % port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
