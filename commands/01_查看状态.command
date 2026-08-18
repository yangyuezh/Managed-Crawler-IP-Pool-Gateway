#!/bin/zsh
set -u

ROOT="${0:A:h:h}"
PYTHON="/opt/anaconda3/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

cd "$ROOT" || exit 1
clear
"$PYTHON" -m crawler_gateway --config private/gateway.yaml status --plain
CODE=$?
echo
read -r "?按回车关闭..."
exit $CODE
