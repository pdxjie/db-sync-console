import unittest

from fastapi import HTTPException

from sync_tool.app import _validate_connection_host


class AppConnectionValidationTests(unittest.TestCase):
    def test_comz_host_suggests_com(self):
        with self.assertRaises(HTTPException) as raised:
            _validate_connection_host("prod", "rm-bp.mysql.rds.aliyuncs.comz")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("rm-bp.mysql.rds.aliyuncs.com", raised.exception.detail)

    def test_host_must_not_include_port(self):
        with self.assertRaises(HTTPException):
            _validate_connection_host("test", "127.0.0.1:3306")


if __name__ == "__main__":
    unittest.main()
