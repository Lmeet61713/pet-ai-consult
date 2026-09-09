#!/bin/bash
# 部署后自检：三场景 + 急症 + 流式 + 任务表（v2 部署包）
set -u
API="${API:-http://127.0.0.1:18100}"
HEADERS=(-H "X-User-Id: smoke" -H "X-Tenant-Id: smoke")
pass=0; fail=0

check() {
  local name="$1" result="$2"
  if [ "$result" = "0" ]; then echo "[PASS] $name"; pass=$((pass+1));
  else echo "[FAIL] $name"; fail=$((fail+1)); fi
}

echo "=== 1. 直答（猫藓，期望 <1s + success） ==="
start=$(date +%s%N)
resp=$(curl -s -m 30 -X POST "$API/api/v1/consult" "${HEADERS[@]}" \
  -F "conversation_id=smoke1" -F "text=猫藓会传染给人吗")
end=$(date +%s%N)
echo "$resp" | grep -q "\"status\":\"success\""
check "直答 success" $?
echo "$resp" | grep -q "\"answer\""
check "直答有回答" $?

echo "=== 2. 追问（狗拉稀怎么办，期望 provisional） ==="
resp=$(curl -s -m 60 -X POST "$API/api/v1/consult" "${HEADERS[@]}" \
  -F "conversation_id=smoke2" -F "text=狗拉稀怎么办")
echo "$resp" | grep -q "\"answer_mode\":\"provisional\""
check "追问模式" $?

echo "=== 3. 详细症状（期望 normal） ==="
resp=$(curl -s -m 60 -X POST "$API/api/v1/consult" "${HEADERS[@]}" \
  -F "conversation_id=smoke3" -F "text=我家狗拉稀两天了，精神还行，食欲正常，没有呕吐，一天拉三次")
echo "$resp" | grep -q "\"answer_mode\":\"normal\""
check "详细回答" $?

echo "=== 4. 急症（期望固定模板） ==="
resp=$(curl -s -m 30 -X POST "$API/api/v1/consult" "${HEADERS[@]}" \
  -F "conversation_id=smoke4" -F "text=我家狗呼吸困难，舌头都紫了")
echo "$resp" | grep -q "\"status\":\"success\""
check "急症秒回" $?

echo "=== 5. 流式（final 必有；token 数在队列化模式下可为 0） ==="
stream_out=$(curl -s -m 60 -N -X POST "$API/api/v1/consult/stream" "${HEADERS[@]}" \
  -F "conversation_id=smoke5" -F "text=我家狗拉稀两天了")
tokens=$(echo "$stream_out" | grep -c "^event: token")
has_final=$(echo "$stream_out" | grep -c "^event: final")
[ "$has_final" -gt 0 ]
check "流式 final 事件（token=$tokens）" $?

echo ""
echo "=== 结果：PASS=$pass FAIL=$fail ==="
[ "$fail" = "0" ]
