# 轨道编队控制台

模拟轨道编队主控切换场景的多数派共识控制台：值班员可创建 3-5 个模拟控制节点、
设置不可达节点、以控制器标识申请任期、携带稳定请求标识提交指令；页面经真实
接口展示唯一现役任期、法定确认节点及各节点已接受的指令序列。

## 快速开始

```bash
# 本地直接运行（Python 3.9+，零依赖）
python3 -m app.server            # 打开 http://localhost:8080

# 或容器运行（可配置宿主机端口）
HOST_PORT=9090 docker compose up --build -d app
curl http://localhost:9090/health
```

## 验收（verify）

```bash
./scripts/verify.sh              # 或 make verify
```

verify 流程：构建可运行产物 → 启动编队服务 → 运行代码测试（多数确认后崩溃、
旧主控迟到栅栏、重传冲突等）→ 对任期申请、指令提交、健康路径执行 HTTP 冒烟
（含宿主机端口健康路径与端到端崩溃恢复）→ 退出并以退出码报告验收结果。

本地无 Docker 时：`make test` 运行代码测试，`make smoke` 本地起服务执行冒烟。

## 场景演练（页面操作）

1. **三节点多数提交**：创建 3 节点编队 → `ctrl-A` 申请任期（多数确认）→ 提交
   指令。页面显示唯一现役任期、法定确认节点，三节点已提交序列一致。
2. **旧主控迟到栅栏**：待 `ctrl-A` 租约失效（默认 1.5s，容器内 `LEASE_MS`）→
   `ctrl-B` 取得更高任期并提交新指令 → `ctrl-A` 以旧任期重试被栅栏拒绝，
   任一节点日志均不新增旧指令。
3. **多数确认后崩溃**：提交指令时勾选“多数确认后模拟退出”→ 服务在响应前退出
   并自动重启 → 刷新后仍显示已提交；以相同请求标识重传返回原结论且不重复
   追加，同一标识改换指令内容返回冲突。
4. **多数不可达**：将多数节点置为不可达 → 任期申请失败，现役任期与各节点日志
   保持不变 → 恢复可达后节点只能追赶不能提交，页面刷新读取收敛状态。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/healthz` | 健康路径（经可配置宿主机端口暴露） |
| GET | `/api/state` | 集群快照：现役任期/确认节点/各节点序列/已提交序列 |
| POST | `/api/formations` | 创建编队 `{"nodeCount": 3..5}` |
| POST | `/api/nodes/<id>/reachability` | 设置可达性 `{"reachable": bool}` |
| POST | `/api/term/request` | 申请任期 `{"controllerId": "..."}` |
| POST | `/api/commands` | 提交指令 `{"controllerId","term","requestId","command","crashAfterMajority"?}` |

错误码：`fenced`（旧任期栅栏）、`conflict`（请求标识冲突）、`no_majority`
（多数不可达）、`no_quorum`（未达多数确认）、`no_active_term`（无有效租约）、
`lease_held`（租约被他人持有）、`not_leader`（非现役持有者）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8080` | 容器内服务端口 |
| `HOST_PORT` | `8080` | Compose 宿主机映射端口（`HOST_PORT=9090 docker compose up -d`） |
| `DATA_DIR` | `./data`（容器 `/data`） | 持久化目录，重启后已提交结果保留 |
| `LEASE_MS` | `4000`（容器 `1500`） | 现役任期租约时长（毫秒） |
| `CRASH_AFTER_MAJORITY` | 关 | 置 `1` 时首个提交在多数确认后、响应前退出 |

## 持久化与一致性

全部状态（各节点日志、已提交序列、请求标识台账、现役任期）原子写入
`$DATA_DIR/cluster.json`；提交在多数确认后先持久化再响应，因此多数确认后
崩溃、重启刷新仍显示已提交；相同请求标识重传命中台账返回原结论。
