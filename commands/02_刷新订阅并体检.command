#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
echo "正在启动独立网关，更新备用池，并检测配置中的全部目标网站。"
echo "窗口会打印运行参数、订阅更新、逐节点检测和主池/备用池事实结果。"
echo
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml start || {
  echo "网关启动失败。"
  read -r "?按回车关闭..."
  exit 1
}
echo
"$PYTHON" -m crawler_gateway --human --config private/gateway.yaml maintain-reserve --once
CODE=$?
echo
"$PYTHON" -m crawler_gateway --config private/gateway.yaml status --plain --skip-nsfc
echo
read -r "?按回车关闭..."
exit $CODE
