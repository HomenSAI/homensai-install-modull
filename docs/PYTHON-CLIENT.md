# Экспериментальный Python-клиент установщика

`homensai-installer-client` версии `0.1.0` — локальный клиент JSON/CLI API,
не сам установщик и не сетевая служба. Версия клиента не совпадает с
minor-версией API: нужная возможность отдельно проверяется через `capabilities`
и `min_minor`.

```python
from homensai_installer_client import InstallerClient, InstallerClientError

client = InstallerClient(
    python_executable='/absolute/path/to/trusted/installer-python',
    api_root='/absolute/path/to/trusted/installer',
    timeout=60,
)
caps = client.capabilities()
result = client.call('validate', {'manifest': manifest}, min_minor=0, language='ru')
```

Клиент требует Python 3.14+, согласует major/minor, операции и язык, передаёт
JSON через stdin/stdout без shell, ограничивает размер ответа и не повторяет
изменяющие операции автоматически. Для архивного секрета разрешён только явно
названный канал `HOMENSAI_SECRET_*`. Python и `api_root` должны быть доверены;
клиент не проверяет их подлинность, а установленный wheel не устанавливает
серверную часть. Старый импорт из `examples.demo_product.installer_client`
остаётся совместимым в исходном дереве.
Без `api_root` клиент ищет установщик рядом с самим пакетом и отказывает, если
его там нет; текущий каталог вызывающей программы для поиска API не используется.

## Локальная проверочная сборка

Подготовьте отдельное окружение Python 3.14 с доверенным pip и заранее
проверенный wheelhouse. Файл `build-requirements.txt` фиксирует setuptools
84.2.1 и SHA-256 его официального wheel. Затем без обращения к сети:

```sh
python -m pip install --no-index --find-links /trusted/wheelhouse \
  --require-hashes --no-deps -r build-requirements.txt
python -m pip wheel --no-index --no-deps --no-build-isolation \
  --wheel-dir /absolute/output .
```

3 ноября 2026 года Linux/Python 3.14: получен и по хэшу PyPI проверен
`setuptools-84.2.1-py3-none-any.whl` SHA-256
`ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2`, затем
offline собран клиентский wheel SHA-256
`7d1bc18fbedaf43f61a4211b0472b42c0f88046c70f0b2eb9d5f6bac7594ae5d`.
Он установлен без сети в отдельную среду и вызвал API из независимого
каталога с пробелами и кириллицей; тестовая подмена `shared.api` не выполнилась.
Этот хэш относится только к конкретному локальному файлу: воспроизводимость,
подпись, публичная поставка и Windows пока не подтверждены.

Первый локальный эксперимент использовал системные setuptools 66.1.1 и wheel
0.38.4, для которых опубликованы предупреждения безопасности; он выполнялся
offline без обработки чужих пакетов и не является выпуском. Для следующей
сборки использован только setuptools 84.2.1, установленный по закреплённому
хэшу. Подлинность источника по одному хэшу не доказывается; перед выпуском
нужны проверка происхождения, актуальных уязвимостей, подпись и проверка состава.

Setuptools указан производителем как MIT; его wheel содержит и встроенные
зависимости с собственными лицензиями. Сборочный инструмент не включён в
клиентский wheel, у клиента нет внешних runtime-зависимостей. В репозитории не
обнаружен файл лицензии самого проекта: условия распространения клиента пока
не определены; выбор и добавление лицензии остаются задачей владельца.
Источники: [setuptools на PyPI](https://pypi.org/project/setuptools/84.2.1/),
[предупреждение setuptools](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf),
[история wheel](https://github.com/pypa/wheel/blob/main/docs/news.rst).
