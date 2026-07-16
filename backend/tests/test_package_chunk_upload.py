import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from sqlmodel import SQLModel, Session, create_engine, select

from backend.api import packages
from backend.models import AppPackage, User


class PackageChunkUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.temp_dir.name) / "uploads" / "apps"
        self.chunk_dir = self.upload_dir / ".chunks"
        self.chunk_dir.mkdir(parents=True)

        self.patchers = [
            patch.object(packages, "UPLOAD_DIR", self.upload_dir),
            patch.object(packages, "CHUNK_UPLOAD_DIR", self.chunk_dir),
            patch(
                "backend.api.packages.parse_apk_info",
                return_value={
                    "app_name": "Demo",
                    "package_name": "com.example.demo",
                    "version_name": "1.0.0",
                    "version_code": "100",
                },
            ),
            patch(
                "backend.api.packages.parse_ipa_info",
                return_value={
                    "app_name": "Demo iOS",
                    "package_name": "com.example.demo.ios",
                    "version_name": "2.0.0",
                    "version_code": "200",
                    "signing_type": "adhoc",
                },
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(username="tester", hashed_password="x", full_name="Tester")
        self.session.add(self.user)
        self.session.commit()
        self.session.refresh(self.user)

    def tearDown(self) -> None:
        self.session.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _payload(self, *, filename: str = "demo.apk", file_size: int = 4):
        return packages.PackageUploadSessionCreate(
            filename=filename,
            file_size=file_size,
            chunk_size=packages.PACKAGE_UPLOAD_CHUNK_SIZE,
            total_chunks=(file_size + packages.PACKAGE_UPLOAD_CHUNK_SIZE - 1)
            // packages.PACKAGE_UPLOAD_CHUNK_SIZE,
        )

    def _upload_file(self, data: bytes, filename: str = "chunk.part") -> UploadFile:
        return UploadFile(file=BytesIO(data), filename=filename)

    def test_create_session_success_and_cancel_cleanup(self):
        response = packages.create_package_upload_session(
            self._payload(file_size=packages.PACKAGE_UPLOAD_CHUNK_SIZE + 1),
            current_user=self.user,
        )

        session_dir = self.chunk_dir / response.upload_id
        self.assertTrue(session_dir.exists())
        self.assertEqual(response.chunk_size, packages.PACKAGE_UPLOAD_CHUNK_SIZE)
        self.assertEqual(response.total_chunks, 2)
        self.assertEqual(response.uploaded_chunks, [])

        packages.cancel_package_upload(response.upload_id, current_user=self.user)
        self.assertFalse(session_dir.exists())

    def test_create_session_rejects_unsupported_extension(self):
        with self.assertRaises(HTTPException) as context:
            packages.create_package_upload_session(
                self._payload(filename="demo.zip"),
                current_user=self.user,
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, "仅支持 .apk 或 .ipa 文件")

    async def test_upload_ipa_chunk_and_complete(self):
        data = b"fake-ipa"
        response = packages.create_package_upload_session(
            self._payload(filename="demo.ipa", file_size=len(data)),
            current_user=self.user,
        )
        await packages.upload_package_chunk(
            response.upload_id,
            0,
            self._upload_file(data),
            current_user=self.user,
        )
        result = packages.complete_package_upload(
            response.upload_id,
            session=self.session,
            current_user=self.user,
        )

        self.assertEqual(result.platform, "ios")
        self.assertEqual(result.package_name, "com.example.demo.ios")
        package = self.session.get(AppPackage, result.id)
        self.assertTrue(package.file_path.endswith(".ipa"))

    async def test_upload_multiple_chunks_and_complete(self):
        data = b"a" * packages.PACKAGE_UPLOAD_CHUNK_SIZE + b"tail"
        response = packages.create_package_upload_session(
            self._payload(file_size=len(data)),
            current_user=self.user,
        )

        first = await packages.upload_package_chunk(
            response.upload_id,
            0,
            self._upload_file(data[: packages.PACKAGE_UPLOAD_CHUNK_SIZE]),
            current_user=self.user,
        )
        second = await packages.upload_package_chunk(
            response.upload_id,
            1,
            self._upload_file(data[packages.PACKAGE_UPLOAD_CHUNK_SIZE :]),
            current_user=self.user,
        )
        result = packages.complete_package_upload(
            response.upload_id,
            session=self.session,
            current_user=self.user,
        )

        self.assertEqual(first.uploaded_chunks_count, 1)
        self.assertEqual(second.uploaded_chunks_count, 2)
        self.assertEqual(result.package_name, "com.example.demo")
        self.assertFalse((self.chunk_dir / response.upload_id).exists())

        package = self.session.exec(select(AppPackage)).one()
        saved_path = self.upload_dir / Path(package.file_path).name
        self.assertEqual(saved_path.read_bytes(), data)

    async def test_complete_rejects_missing_chunk(self):
        data = b"a" * packages.PACKAGE_UPLOAD_CHUNK_SIZE + b"b"
        response = packages.create_package_upload_session(
            self._payload(file_size=len(data)),
            current_user=self.user,
        )
        await packages.upload_package_chunk(
            response.upload_id,
            0,
            self._upload_file(data[: packages.PACKAGE_UPLOAD_CHUNK_SIZE]),
            current_user=self.user,
        )

        with self.assertRaises(HTTPException) as context:
            packages.complete_package_upload(
                response.upload_id,
                session=self.session,
                current_user=self.user,
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("缺少分片", context.exception.detail)

    async def test_chunk_index_out_of_range_is_rejected(self):
        response = packages.create_package_upload_session(
            self._payload(file_size=4),
            current_user=self.user,
        )

        with self.assertRaises(HTTPException) as context:
            await packages.upload_package_chunk(
                response.upload_id,
                1,
                self._upload_file(b"demo"),
                current_user=self.user,
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, "分片索引越界")

    async def test_reuploading_same_chunk_overwrites_previous_part(self):
        response = packages.create_package_upload_session(
            self._payload(file_size=4),
            current_user=self.user,
        )

        await packages.upload_package_chunk(
            response.upload_id,
            0,
            self._upload_file(b"old!"),
            current_user=self.user,
        )
        await packages.upload_package_chunk(
            response.upload_id,
            0,
            self._upload_file(b"good"),
            current_user=self.user,
        )
        package = packages.complete_package_upload(
            response.upload_id,
            session=self.session,
            current_user=self.user,
        )

        saved_path = self.upload_dir / Path(package.file_path).name
        self.assertEqual(saved_path.read_bytes(), b"good")
