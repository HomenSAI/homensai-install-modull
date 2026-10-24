#!/bin/sh
# HomenS.AI · © 2026 Serhii Khomenko
# Сгенерированный текст; генератор этот сценарий не исполнял.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Требуются права root' >&2
    exit 1
fi

# Данные, программа, runtime, архивы, секреты и учётная запись сохраняются.
systemctl stop -- homensai-rent-demo-core-server.service
systemctl disable -- homensai-rent-demo-core-server.service
rm -f -- /etc/systemd/system/homensai-rent-demo-core-server.service.d/override.conf /etc/systemd/system/homensai-rent-demo-core-server.service
rmdir -- /etc/systemd/system/homensai-rent-demo-core-server.service.d 2>/dev/null || :
systemctl daemon-reload
