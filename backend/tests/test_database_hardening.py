import tempfile
import unittest
from pathlib import Path

from sqlmodel import create_engine

from backend.database import SQLITE_BUSY_TIMEOUT_MS, configure_sqlite_engine


class SqliteEngineHardeningTests(unittest.TestCase):
    def test_configure_sqlite_engine_applies_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "hardening.db"
            engine = create_engine(
                f"sqlite:///{db_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            configure_sqlite_engine(engine)

            with engine.connect() as conn:
                journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
                synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
                busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

            self.assertEqual(str(journal_mode).lower(), "wal")
            # synchronous: 1 == NORMAL
            self.assertEqual(int(synchronous), 1)
            self.assertEqual(int(busy_timeout), SQLITE_BUSY_TIMEOUT_MS)

            engine.dispose()

    def test_wal_mode_persists_for_new_connections(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "hardening.db"
            engine = create_engine(
                f"sqlite:///{db_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
            configure_sqlite_engine(engine)
            with engine.connect() as conn:
                conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            engine.dispose()

            # WAL 写入 DB 文件头，对未附加 listener 的新 engine 也生效
            plain_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
            with plain_engine.connect() as conn:
                journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            self.assertEqual(str(journal_mode).lower(), "wal")
            plain_engine.dispose()


if __name__ == "__main__":
    unittest.main()
