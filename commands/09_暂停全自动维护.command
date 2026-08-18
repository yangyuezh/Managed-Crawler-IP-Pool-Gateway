#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "正在暂停后台自动维护。节点、检测结果和正式数据都不会删除。"
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml stop-maintenance
CODE=$?
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml status --plain --skip-nsfc
echo
read -r "?按回车关闭..."
exit $CODE
