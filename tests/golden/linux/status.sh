#!/bin/sh
# HomenS.AI · © 2026 Serhii Khomenko
# Сгенерированный текст; генератор этот сценарий не исполнял.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

failed=0
systemctl show --no-pager --property=Id,LoadState,ActiveState,SubState -- homensai-rent-demo-core-server.service
systemctl is-active --quiet -- homensai-rent-demo-core-server.service || failed=1
exit "$failed"
