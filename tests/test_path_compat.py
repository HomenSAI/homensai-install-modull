"""Проверки безопасных путей на поддержанных Python."""
import unittest
from shared.datahome import _windows_reserved, checked_path, DataHomeError

class ReservedPathCompatibilityTests(unittest.TestCase):
    def test_reserved_names_and_windows_devices(self):
        for value in ("CON", "nul.txt", "a/COM1.log", "a/LPT¹", "bad?name", "name. "):
            with self.subTest(value=value):
                self.assertTrue(_windows_reserved(value))
        self.assertFalse(_windows_reserved("ordinary/path.with.dots"))

    def test_paths_stay_strict(self):
        for value in ("/tmp/a/../b", "/tmp/NUL.txt", "/tmp/name. ", "/tmp/file:stream"):
            with self.subTest(value=value), self.assertRaises(DataHomeError):
                checked_path(value)


if __name__ == "__main__":
    unittest.main()
