#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 server.py --open
printf '\n按回车关闭窗口。'
read -r answer
