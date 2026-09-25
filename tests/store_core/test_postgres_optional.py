"""The offline core must import on a host without a PostgreSQL driver (CI installs none)."""
import subprocess
import sys
import unittest


class PostgresDriverIsOptionalTests(unittest.TestCase):
    def test_package_imports_without_psycopg2(self):
        code = ("import sys; sys.modules['psycopg2'] = None; sys.modules['psycopg2.extras'] = None\n"
                "import packages.store_core as core\n"
                "try:\n    core.TenantAwarePostgresRepository(None, 't')\nexcept RuntimeError as e:\n    print(e)\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("needs psycopg2", out.stdout)


if __name__ == "__main__":
    unittest.main()
