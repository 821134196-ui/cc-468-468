#!/usr/bin/env bash
# 一条本地启动命令：重置数据库，载入部署 / 缺件 / 提前解除演示样例。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
SCENARIO="${SCENARIO:-yellow}"

exec python3 -m app --reseed --host 0.0.0.0 --port "$PORT" --scenario "$SCENARIO"
