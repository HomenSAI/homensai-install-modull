"""Подключаемые словари: данные без исполнения и строгие переменные."""
import json
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
