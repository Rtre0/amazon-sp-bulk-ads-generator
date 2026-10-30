#!/usr/bin/env bash
# 一键跑全部验收测试（v10）。
#   - test_1..3、test_5：纯 Python，直接运行
#   - test_4：需要后端已在 http://127.0.0.1:8000 运行
#   - test_6：需要 jsdom（受管 node 工作区）+ tests/_vendor/vue.global.prod.js
# v9 重点：test_2 的 [0] 段会把 BULK_COLUMNS 与 samples/AdvertisingBulksheetTemplate-seller.xlsx
#          的官方 31 列表头逐字比对，任何列名漂移都会被拦下。
set -u
cd "$(dirname "$0")/.."

PY="${PY:-C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe}"
NODE="${NODE:-C:/Users/Administrator/.workbuddy-ai/binaries/node/versions/22.22.2-6/node.exe}"
NODE_MODULES="${NODE_MODULES:-C:/Users/Administrator/.workbuddy-ai/binaries/node/workspace/node_modules}"

total_pass=0
total_fail=0
failed_files=()

run_py () {
  local f="$1"
  local out
  out="$(PYTHONIOENCODING=utf-8 "$PY" "$f" 2>&1)"
  local line
  line="$(printf '%s\n' "$out" | grep -E '^结果：' | tail -1)"
  printf '%-46s %s\n' "$(basename "$f")" "${line:-未产出结果}"
  if [ -z "$line" ]; then
    printf '%s\n' "$out" | tail -12
    failed_files+=("$f")
    return
  fi
  local p fa
  p="$(printf '%s' "$line" | sed -E 's/.*PASS ([0-9]+).*/\1/')"
  fa="$(printf '%s' "$line" | sed -E 's/.*FAIL ([0-9]+).*/\1/')"
  total_pass=$((total_pass + p))
  total_fail=$((total_fail + fa))
  [ "$fa" != "0" ] && failed_files+=("$f")
  return 0
}

echo "==================================================================="
echo " Amazon Bulk Ads Generator · 全量验收测试 (v10)"
echo "==================================================================="

for f in tests/test_1_noun_matching.py tests/test_2_end_to_end.py \
         tests/test_3_dictionary_io.py tests/test_4_http_smoke.py \
         tests/test_5_v7_quantity_and_rematch.py; do
  run_py "$f"
done

if [ -d "$NODE_MODULES/jsdom" ] && [ -f tests/_vendor/vue.global.prod.js ]; then
  out="$(NODE_PATH="$NODE_MODULES" "$NODE" tests/test_6_frontend_refresh.js 2>&1)"
  line="$(printf '%s\n' "$out" | grep -E '^结果：' | tail -1)"
  printf '%-46s %s\n' "test_6_frontend_refresh.js" "${line:-未产出结果}"
  if [ -n "$line" ]; then
    p="$(printf '%s' "$line" | sed -E 's/.*PASS ([0-9]+).*/\1/')"
    fa="$(printf '%s' "$line" | sed -E 's/.*FAIL ([0-9]+).*/\1/')"
    total_pass=$((total_pass + p))
    total_fail=$((total_fail + fa))
    [ "$fa" != "0" ] && failed_files+=("tests/test_6_frontend_refresh.js")
  else
    failed_files+=("tests/test_6_frontend_refresh.js")
  fi
else
  printf '%-46s %s\n' "test_6_frontend_refresh.js" "[SKIP] 缺少 jsdom 或 vue.global.prod.js"
fi

echo "-------------------------------------------------------------------"
echo " 合计：PASS ${total_pass}   FAIL ${total_fail}"
if [ "${#failed_files[@]}" -gt 0 ]; then
  echo " 失败文件：${failed_files[*]}"
  exit 1
fi
echo " 全部通过"
