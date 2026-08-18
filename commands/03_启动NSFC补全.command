#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "正在按 gateway.yaml 配置检查独立工作通道。窗口保持打开时会显示爬虫运行信息。"
echo
"$PYTHON" - <<'PY'
from pathlib import Path
from crawler_gateway.integrations.nsfc import DEFAULT_NSFC_DATA, existing_nsfc_processes

processes = existing_nsfc_processes(Path(DEFAULT_NSFC_DATA))
if processes:
    print("已有 NSFC 进程正在使用同一数据目录，未开始节点体检：")
    for item in processes:
        print(f"  PID {item['pid']} ({item['role']})")
    print("请先双击 04_停止NSFC和网关.command，再重新启动。")
    raise SystemExit(2)
PY
PRECHECK=$?
if [[ $PRECHECK -ne 0 ]]; then
  echo
  read -r "?按回车关闭..."
  exit $PRECHECK
fi
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml start || {
  echo "网关启动失败。"
  read -r "?按回车关闭..."
  exit 1
}
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml run-nsfc
CODE=$?
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml status --plain
echo
read -r "?按回车关闭..."
exit $CODE
