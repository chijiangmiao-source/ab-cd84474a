#!/bin/sh
# 验收：构建可运行产物 -> 启动服务 -> 运行 verify（代码测试 + HTTP 冒烟）-> 以退出码报告
set -eu
cd "$(dirname "$0")/.."

export HOST_PORT="${HOST_PORT:-8080}"

cleanup() {
  docker compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 构建可运行产物 =="
docker compose build

echo "== 启动编队服务（宿主机端口 $HOST_PORT，健康路径 /health）=="
docker compose up -d app

echo "== 运行 verify：代码测试 + HTTP 冒烟 =="
if docker compose run --rm verify; then
  echo "== 验收通过 =="
else
  code=$?
  echo "== 验收失败（退出码 $code）==" >&2
  exit "$code"
fi
