#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "暂停后台自动维护，并停止 NSFC 详情爬虫、守护进程和本项目代理通道。不会删除任何数据。"
"$PYTHON" -m crawler_gateway --config private/gateway.yaml stop-maintenance
"$PYTHON" -m crawler_gateway --config private/gateway.yaml stop-nsfc
"$PYTHON" -m crawler_gateway --config private/gateway.yaml stop
CODE=$?
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml status --plain
echo
read -r "?按回车关闭..."
exit $CODE
