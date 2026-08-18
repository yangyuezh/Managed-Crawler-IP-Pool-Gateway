#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "正在安装并启动后台自动维护。"
echo "完成后可以关闭本窗口；登录本机后程序会自动更新订阅、检测节点并恢复网关。"
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml install-service
CODE=$?
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml status --plain --skip-nsfc
echo
read -r "?按回车关闭..."
exit $CODE
