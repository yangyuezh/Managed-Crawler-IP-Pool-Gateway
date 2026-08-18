#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "临时前台持续维护：按配置周期更新订阅，再检测全部目标网站。"
echo "保持窗口打开即可持续运行；长期无人值守请使用 08_启用全自动维护.command。"
echo
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml start || exit 1
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml maintain-reserve
CODE=$?
echo
read -r "?按回车关闭..."
exit $CODE
