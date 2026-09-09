#!/usr/bin/env bash
# 问诊系统（pet-consult）独立 RocketMQ 实例管理脚本。
#
# 背景：审核系统 pet-moderation 已占用 namesrv :9876 / broker :10911
# （集群名 pet-moderation）。问诊 Phase 2 队列化使用独立实例：
#   namesrv :9877 / broker :10912，集群名 pet-consult，数据目录独立。
# 两边完全隔离：互不重启、互不占端口、互不抢消费组。
#
# 用法: bash scripts/rocketmq_consult.sh {start|stop|restart|status}
set -euo pipefail

SERVICE_ROOT=/root/autodl-tmp/services
ROCKETMQ_HOME="${SERVICE_ROOT}/rocketmq/rocketmq-all-5.3.2-bin-release"
ROCKETMQ_CONFIG="${SERVICE_ROOT}/rocketmq-consult-broker.conf"
NAMESRV_CONFIG="${SERVICE_ROOT}/rocketmq-consult-namesrv.conf"
DATA_ROOT="${SERVICE_ROOT}/rocketmq-data-consult"
LOG_DIR="${DATA_ROOT}/logs"
RUN_DIR="${SERVICE_ROOT}/pet-consult-mq-run"
NAMESRV_PORT=9877
BROKER_PORT=10912
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

pidfile() { echo "${RUN_DIR}/$1.pid"; }

read_pid() {
  local f; f="$(pidfile "$1")"
  [[ -f "$f" ]] && cat "$f" || true
}

is_up() {
  local pid; pid="$(read_pid "$1")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

wait_port() {
  local port="$1"
  for _ in $(seq 1 40); do
    if timeout 1 bash -c "</dev/tcp/127.0.0.1/${port}" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_namesrv() {
  if is_up namesrv; then
    echo "namesrv already running (pid $(read_pid namesrv))"
    return 0
  fi
  (
    cd "${ROCKETMQ_HOME}"
    nohup env JAVA_OPT_EXT='-Xms256m -Xmx256m -Xmn128m' \
      bin/mqnamesrv -c "${NAMESRV_CONFIG}" >"${LOG_DIR}/namesrv.log" 2>&1 &
    echo $! >"$(pidfile namesrv)"
  )
  if wait_port "${NAMESRV_PORT}"; then
    echo "namesrv ready on :${NAMESRV_PORT}"
  else
    echo "namesrv failed to start on :${NAMESRV_PORT}; see ${LOG_DIR}/namesrv.log" >&2
    return 1
  fi
}

start_broker() {
  if is_up broker; then
    echo "broker already running (pid $(read_pid broker))"
    return 0
  fi
  if [[ ! -f "${ROCKETMQ_CONFIG}" ]]; then
    echo "Missing ${ROCKETMQ_CONFIG}" >&2
    return 1
  fi
  (
    cd "${ROCKETMQ_HOME}"
    nohup env NAMESRV_ADDR="127.0.0.1:${NAMESRV_PORT}" \
      JAVA_OPT_EXT='-Xms512m -Xmx512m -XX:MaxDirectMemorySize=1g' \
      bin/mqbroker -c "${ROCKETMQ_CONFIG}" >"${LOG_DIR}/broker.log" 2>&1 &
    echo $! >"$(pidfile broker)"
  )
  if wait_port "${BROKER_PORT}"; then
    echo "broker ready on :${BROKER_PORT} (cluster pet-consult)"
  else
    echo "broker failed to start on :${BROKER_PORT}; see ${LOG_DIR}/broker.log" >&2
    return 1
  fi
}

stop_one() {
  local name="$1"
  if is_up "$name"; then
    kill "$(read_pid "$name")" 2>/dev/null || true
    for _ in $(seq 1 20); do
      is_up "$name" || break
      sleep 1
    done
    if is_up "$name"; then
      echo "force-killing $name"
      kill -9 "$(read_pid "$name")" 2>/dev/null || true
    fi
    rm -f "$(pidfile "$name")"
    echo "$name stopped"
  else
    echo "$name not running"
    rm -f "$(pidfile "$name")"
  fi
  # 兜底：pid 文件可能只记录了 shell wrapper，Java 子进程会变孤儿；
  # 按专属配置路径精确清理（不会误伤审核系统实例）。
  # 本脚本以文件方式调用，自身 cmdline 不含配置路径，不会被匹配。
  pkill -f "rocketmq-consult-${name}.conf" 2>/dev/null || true
}

status() {
  for name in namesrv broker; do
    if is_up "$name"; then
      echo "$name: running (pid $(read_pid "$name"))"
    else
      echo "$name: stopped"
    fi
  done
  for port in "${NAMESRV_PORT}" "${BROKER_PORT}"; do
    if timeout 1 bash -c "</dev/tcp/127.0.0.1/${port}" 2>/dev/null; then
      echo "port ${port}: listening"
    else
      echo "port ${port}: unavailable"
    fi
  done
}

case "${1:-}" in
  start)
    start_namesrv
    start_broker
    status
    ;;
  stop)
    stop_one broker
    stop_one namesrv
    ;;
  restart)
    stop_one broker
    stop_one namesrv
    start_namesrv
    start_broker
    status
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
