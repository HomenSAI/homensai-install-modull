"""Подключаемые словари: данные без исполнения и строгие переменные."""
import json
import os
from pathlib import Path

import pytest

from shared.i18n import LanguageError, translate


@pytest.mark.parametrize('language,expected', [
    ('en', 'Ready to install Demo.'),
    ('ru', 'Всё готово к установке Demo.'),
    ('de', 'Bereit zur Installation von Demo.'),
])
def test_translated_message(language, expected):
    assert translate(language, 'installer.ready', product='Demo') == expected


@pytest.mark.parametrize('language', ['../ru', 'RU', 'ru.json', 'xx', '', None])
def test_invalid_or_unavailable_language(language):
    with pytest.raises(LanguageError):
        translate(language, 'installer.ready', product='Demo')


def test_variable_contract_and_key():
    for key, values in [('installer.ready', {}), ('installer.ready', {'product': 'Demo', 'extra': 'x'}),
                        ('installer.ready', {'product': 1}), ('unknown', {})]:
        with pytest.raises(LanguageError):
            translate('en', key, **values)


def test_new_language_is_data_only(monkeypatch, tmp_path):
    import shared.i18n as i18n
    source = Path(i18n.__file__).with_name('locales')
    (tmp_path / 'en.json').write_bytes((source / 'en.json').read_bytes())
    catalog = json.loads((source / 'en.json').read_text(encoding='utf-8'))
    catalog['installer.ready'] = 'Example {product}'
    (tmp_path / 'fr.json').write_text(json.dumps(catalog), encoding='utf-8')
    monkeypatch.setattr(i18n, '_DIRECTORY', tmp_path)
    assert translate('fr', 'installer.ready', product='Demo') == 'Example Demo'
    catalog['installer.ready'] = '{product.__class__}'
    (tmp_path / 'fr.json').write_text(json.dumps(catalog), encoding='utf-8')
    with pytest.raises(LanguageError):
        translate('fr', 'installer.ready', product='Demo')
    catalog['installer.ready'] = 'No variable'
    (tmp_path / 'fr.json').write_text(json.dumps(catalog), encoding='utf-8')
    with pytest.raises(LanguageError):
        translate('fr', 'installer.ready', product='Demo')
    catalog['installer.ready'] = '{product}\ud800'
    (tmp_path / 'fr.json').write_text(json.dumps(catalog), encoding='utf-8')
    with pytest.raises(LanguageError):
        translate('fr', 'installer.ready', product='Demo')


@pytest.mark.skipif(os.name != 'posix', reason='POSIX links and FIFO')
def test_language_discovery_rejects_links_and_fifo_without_blocking(monkeypatch, tmp_path):
    import shared.i18n as i18n
    source = Path(i18n.__file__).with_name('locales') / 'en.json'
    (tmp_path / 'en.json').write_bytes(source.read_bytes())
    (tmp_path / 'fr.json').symlink_to(source)
    os.mkfifo(tmp_path / 'de.json')
    monkeypatch.setattr(i18n, '_DIRECTORY', tmp_path)
    assert i18n.available_languages() == ['en']
    for language in ('fr', 'de'):
        with pytest.raises(LanguageError):
            translate(language, 'installer.ready', product='Demo')


def test_language_replacement_during_open_is_rejected(monkeypatch, tmp_path):
    import shared.i18n as i18n
    source = Path(i18n.__file__).with_name('locales') / 'en.json'
    content = source.read_bytes()
    (tmp_path / 'en.json').write_bytes(content)
    target = tmp_path / 'fr.json'
    target.write_bytes(content)
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(content)
    monkeypatch.setattr(i18n, '_DIRECTORY', tmp_path)
    original_open = os.open
    def replace_after_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == target:
            os.replace(replacement, target)
        return descriptor
    monkeypatch.setattr(i18n.os, 'open', replace_after_open)
    with pytest.raises(LanguageError):
        translate('fr', 'installer.ready', product='Demo')


@pytest.mark.parametrize('broken', [
    b'{}', b'[]', b'{', b'\xff', b'{"api.invalid_request":"x","api.invalid_request":"y"}',
    b'{"api.invalid_request":42}', b' ' * 65537,
])
def test_invalid_language_files_do_not_affect_other_languages(monkeypatch, tmp_path, broken):
    import shared.i18n as i18n
    source = Path(i18n.__file__).with_name('locales')
    for language in ('en', 'ru', 'de'):
        (tmp_path / (language + '.json')).write_bytes((source / (language + '.json')).read_bytes())
    (tmp_path / 'fr.json').write_bytes(broken)
    monkeypatch.setattr(i18n, '_DIRECTORY', tmp_path)
    assert i18n.available_languages() == ['de', 'en', 'ru']
    with pytest.raises(LanguageError):
        translate('fr', 'installer.ready', product='Demo')
    assert translate('de', 'installer.ready', product='Demo') == 'Bereit zur Installation von Demo.'


@pytest.mark.parametrize('language', ['en', 'ru', 'de'])
def test_installed_catalogs_match_base_keys_and_placeholder_sets(language):
    import shared.i18n as i18n
    source = Path(i18n.__file__).with_name('locales')
    catalog = json.loads((source / (language + '.json')).read_text(encoding='utf-8'))
    result = i18n.validate_catalog(catalog)
    assert result['valid'] and result['message_count'] == len(catalog)
    for key, template in catalog.items():
        fields = i18n._fields(template)
        variables = {name: 'synthetic-value' for name in fields}
        assert isinstance(translate(language, key, **variables), str)
