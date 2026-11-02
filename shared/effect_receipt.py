# HomenS.AI · © 2026 Serhii Khomenko
"""Ограниченное чтение служебной отметки; не аутентифицирует её владельца."""
import json
import os
import stat

from shared.update_plan import EffectIndeterminate, UpdateError, _object

MAX_RECEIPT = 8192


def read_effect_receipt(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise EffectIndeterminate('Недоступна отметка операции') from None
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise EffectIndeterminate('Недопустимая отметка операции')
            raw = stream.read(MAX_RECEIPT + 1)
        if len(raw) > MAX_RECEIPT:
            raise EffectIndeterminate('Недопустимая отметка операции')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
        # None зарезервирован только для отсутствующего файла. JSON null и
        # другие значения не должны давать разрешение повторить эффект.
        if type(value) is not dict:
            raise EffectIndeterminate('Недопустимая отметка операции')
        return value
    except (OSError, ValueError, UnicodeError, RecursionError, UpdateError):
        raise EffectIndeterminate('Недоступна отметка операции') from None
