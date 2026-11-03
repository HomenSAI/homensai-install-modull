"""Безопасная ZIP-заготовка для структурных тестов, не устанавливаемый wheel."""
import io
import zipfile


def structural_wheel():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('fictional_demo/__init__.py', '# synthetic, never executed')
    return data.getvalue()
