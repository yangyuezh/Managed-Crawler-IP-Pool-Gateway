#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "只检测当前备用池对配置中全部目标网站的可用性，不刷新订阅。"
echo
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml start || exit 1
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml probe-reserve
CODE=$?
echo
read -r "?按回车关闭..."
exit $CODE
