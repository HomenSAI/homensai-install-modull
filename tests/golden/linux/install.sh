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

# Bundle должен содержать .service и .override.conf рядом с install.sh.
bundle_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
if ! getent group homensai >/dev/null; then
    groupadd --system -- homensai
fi
if ! getent passwd homensai >/dev/null; then
    useradd --system --gid homensai --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin -- homensai
fi
if [ "$(id -u -- homensai)" -eq 0 ]; then
    printf '%s\n' 'Учётная запись имеет UID 0' >&2
    exit 1
fi
install -d -m 0755 -o root -g root -- /srv/demo/demo-layout/app
install -d -m 0755 -o root -g root -- /srv/demo/demo-layout/releases
install -d -m 0755 -o root -g root -- /srv/demo/demo-layout/runtime
install -d -m 0755 -o root -g root -- /srv/demo/demo-layout/services
install -d -m 0700 -o homensai -g homensai -- /srv/demo/demo-layout/data
install -d -m 0700 -o homensai -g homensai -- /srv/demo/demo-layout/backup
install -d -m 0700 -o homensai -g homensai -- /srv/demo/demo-layout/config
install -d -m 0700 -o root -g root -- /etc/homensai-rent-demo
if [ -L /etc/homensai-rent-demo/service.env ] || { [ -e /etc/homensai-rent-demo/service.env ] && [ ! -f /etc/homensai-rent-demo/service.env ]; }; then
    printf '%s\n' 'Небезопасный тип файла окружения' >&2
    exit 1
fi
if [ ! -e /etc/homensai-rent-demo/service.env ]; then
    install -m 0600 -o root -g root -- /dev/null /etc/homensai-rent-demo/service.env
    printf '%s\n' '# BACKUP_PASSWORD' >> /etc/homensai-rent-demo/service.env
    printf '%s\n' '# BOT_TOKEN' >> /etc/homensai-rent-demo/service.env
    printf '%s\n' '# ENCRYPTION_KEY' >> /etc/homensai-rent-demo/service.env
fi
chown root:root -- /etc/homensai-rent-demo/service.env
chmod 0600 -- /etc/homensai-rent-demo/service.env
install -d -m 0755 -o root -g root -- /etc/systemd/system
install -m 0644 -o root -g root -- "$bundle_dir"/homensai-rent-demo-core-server.service /etc/systemd/system/homensai-rent-demo-core-server.service
install -d -m 0755 -o root -g root -- /etc/systemd/system/homensai-rent-demo-core-server.service.d
install -m 0644 -o root -g root -- "$bundle_dir"/homensai-rent-demo-core-server.service.override.conf /etc/systemd/system/homensai-rent-demo-core-server.service.d/override.conf
printf '%s\n' 'Файлы подготовлены. Заполните защищённый EnvironmentFile вручную.'
printf '%s\n' 'Payload, runtime, миграции и запуск служб требуют отдельной приёмки.'
