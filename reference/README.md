# Reference product

Небольшой реальный web server + SQLite. Только синтетические данные, без email,
Telegram, платежей и внешней сети. Web и migrations используют runtime из поставки.

Сборщик `packaging/build_reference.py` создаёт настоящий wheel, устанавливает его
в изолированный bundled Python, закрепляет hashes и подписывает полный payload
тестовым ключом. Приватный ключ остаётся только в памяти сборочного процесса.
Ключ проверки встраивается в frozen bootstrap. Такая сборка имеет development
marker и не является коммерческим production release.

Текущая reference версия поддерживает Linux x64 user-session. Windows/Docker
и автоматический старт как служба ОС требуют отдельного принятия. Пользователь
не должен устанавливать Python для запуска frozen Linux bootstrap.

EULA в `legal/` — согласованные черновики ru/en/de для reference-проверок.
Выпуск реального продукта блокируется до определения издателя, product license,
юрисдикции, обязательств поддержки и юридического утверждения текстов.
