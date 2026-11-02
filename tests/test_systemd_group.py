"""Порядок systemd units и отказ при неподтверждённой остановке."""
from copy import deepcopy
from pathlib import Path

import pytest

from shared.manifest import load_manifest
from shared.systemd_group import SystemdGroupError, SystemdUserGroup
from shared.systemd_user import SystemdUserAdapter

MANIFEST = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
ORDER = ('web', 'bot', 'scheduler')


class Fake(SystemdUserAdapter):
    def __init__(self, name, events, *, fail=None):
        self.name = name
        self.unit = MANIFEST['product_id'] + '-' + name + '.service'
        self.events, self.fail, self.active = events, fail, True
    def command(self, action, *, execute=False):
        if not execute:
            return {'unit': self.unit, 'action': action, 'scope': 'user'}
        self.events.append((action, self.name))
        if self.fail == action:
            raise RuntimeError('SECRET-SENTINEL')
        self.active = action == 'start'
        return {'unit': self.unit, 'active_state': 'active' if self.active else 'inactive'}
    def inspect(self):
        self.events.append(('inspect', self.name))
        return {'unit': self.unit, 'active_state': 'active' if self.active else 'inactive'}
    def confirm_stopped(self):
        self.events.append(('confirm', self.name))
        if self.active:
            raise RuntimeError('SECRET-SENTINEL')
        return {'unit': self.unit, 'active_state': 'inactive'}


def group(events, fail=None):
    adapters = {name: Fake(name, events, fail=fail if name == 'bot' else None) for name in ORDER}
    return SystemdUserGroup(MANIFEST, modes=('autonomous',), adapters=adapters), adapters


def test_group_dry_run_has_order_but_no_effects():
    events = []
    subject, _ = group(events)
    assert [entry['unit'] for entry in subject.command('start')] == [MANIFEST['product_id'] + '-' + x + '.service' for x in ORDER]
    assert [entry['unit'] for entry in subject.command('stop')] == [MANIFEST['product_id'] + '-' + x + '.service' for x in reversed(ORDER)]
    assert events == []


def test_stop_in_reverse_then_recheck_all_and_start_forward():
    events = []
    subject, _ = group(events)
    assert all(item['active_state'] == 'inactive' for item in subject.command('stop', execute=True))
    assert events == [('inspect', 'web'), ('inspect', 'bot'), ('inspect', 'scheduler'),
                      ('stop', 'scheduler'), ('stop', 'bot'), ('stop', 'web'),
                      ('confirm', 'web'), ('confirm', 'bot'), ('confirm', 'scheduler')]
    events.clear()
    assert all(item['active_state'] == 'active' for item in subject.command('start', execute=True))
    assert events == [('inspect', 'web'), ('inspect', 'bot'), ('inspect', 'scheduler'),
                      ('start', 'web'), ('start', 'bot'), ('start', 'scheduler'),
                      ('inspect', 'web'), ('inspect', 'bot'), ('inspect', 'scheduler')]


def test_failed_stop_stops_no_more_and_returns_safe_error():
    events = []
    subject, _ = group(events, fail='stop')
    with pytest.raises(SystemdGroupError) as error:
        subject.command('stop', execute=True)
    assert events == [('inspect', 'web'), ('inspect', 'bot'), ('inspect', 'scheduler'),
                      ('stop', 'scheduler'), ('stop', 'bot')]
    assert 'SECRET-SENTINEL' not in str(error.value)


@pytest.mark.parametrize('action', ['start', 'stop'])
@pytest.mark.parametrize('reason', ['untrusted', 'transition'])
def test_group_preflight_rejects_invalid_component_before_any_action(action, reason):
    events = []
    subject, adapters = group(events)
    def invalid():
        events.append(('inspect', 'scheduler'))
        if reason == 'untrusted':
            raise RuntimeError('SECRET-SENTINEL')
        return {'unit': adapters['scheduler'].unit, 'active_state': 'activating'}
    adapters['scheduler'].inspect = invalid
    with pytest.raises(SystemdGroupError) as error:
        subject.command(action, execute=True)
    assert 'SECRET-SENTINEL' not in str(error.value)
    assert not any(event[0] in ('start', 'stop') for event in events)


def test_race_during_final_check_refuses_success():
    events = []
    subject, adapters = group(events)
    original = adapters['web'].confirm_stopped
    def changed():
        adapters['web'].active = True
        return original()
    adapters['web'].confirm_stopped = changed
    with pytest.raises(SystemdGroupError):
        subject.command('stop', execute=True)
    assert ('confirm', 'web') in events


def test_rejects_missing_or_wrong_adapter_without_effects():
    events = []
    _, adapters = group(events)
    for modified in ({'web': adapters['web']}, {**adapters, 'web': adapters['bot']}):
        with pytest.raises(SystemdGroupError):
            SystemdUserGroup(deepcopy(MANIFEST), modes=('autonomous',), adapters=modified)
    assert events == []
