#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "只更新订阅和备用节点库存，不检测目标网站，也不调整主工作通道。"
echo
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml start || exit 1
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml refresh-reserve
CODE=$?
echo
read -r "?按回车关闭..."
exit $CODE
