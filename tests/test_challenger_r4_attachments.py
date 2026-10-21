"""Empirical adversarial stress test suite for Requirement R4 (Task Attachments Upload API).

Adversarially tests:
1. Path traversal attacks:
   - Filenames containing '../../etc/passwd', '/etc/shadow', '..\\..\\test.txt', 'foo/../../bar.txt',
     '../../../../../../../../../../../../tmp/pwned.txt', '..', '.', '////', and Windows paths.
   - Verifies the file is strictly written inside artifact_root / {task_id} / artifacts / with a
     sanitized base name, without escaping the directory.
2. Large file upload:
   - Upload 5 MB binary file (os.urandom(5 * 1024 * 1024)).
   - Verifies upload succeeds (HTTP 201), SHA256 checksum matches byte-for-byte on disk,
     response returns correct size (5,242,880 bytes), and no temporary files linger.
3. Empty file upload (0 bytes):
   - Verifies upload succeeds (HTTP 201) and records 0 size in response, disk, and task metadata.
4. Duplicate upload & atomic replacement:
   - Upload file 'data.bin' (size A), then upload 'data.bin' again (size B).
   - Verifies the disk file is atomically replaced with size B content.
   - Verifies task metadata reflects the new size B and no duplicate attachment entries are created.
   - Verifies replacement succeeds even when pre-existing destination file is marked read-only (0o444).
5. Non-existent task:
   - Verifies uploading to non-existent task ID (or non-existent numeric ID) returns 404 Not Found
     and creates no artifacts on disk.
6. Multi-file interleaved upload & metadata consistency:
   - Uploading multiple distinct files followed by duplicate re-upload preserves order and distinct entries.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid

import pytest
from fastapi.testclient import TestClient

from kuzya.api import create_app


@pytest.mark.parametrize(
    "attack_filename, expected_sanitized_name",
    [
        ("../../etc/passwd", "passwd"),
        ("/etc/shadow", "shadow"),
        (r"..\..\test.txt", "....test.txt"),
        ("foo/../../bar.txt", "bar.txt"),
        ("../../../../../../../../../../../../tmp/pwned.txt", "pwned.txt"),
        ("..", "attachment"),
        (".", "attachment"),
        ("///", "attachment"),
        (r"C:\windows\system32\cmd.exe", "cmd.exe"),
        (r"\\server\share\exploit.dll", "exploit.dll"),
        ("nested/deep/directory/secret.key", "secret.key"),
        ("evil\x00bypass.bin", "evil%00bypass.bin"),
        ("   ", "attachment"),
    ],
)
def test_adversarial_path_traversal_sanitization(
    migrated_db: Path,
    tmp_path: Path,
    attack_filename: str,
    expected_sanitized_name: str,
) -> None:
    """Stress-test path traversal vectors to verify strict containment within task artifacts dir."""
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        # Create a task
        create_res = client.post("/tasks", json={"title": "Path Traversal Target"})
        assert create_res.status_code == 201
        task = create_res.json()
        task_id = task["id"]

        expected_artifacts_dir = (artifact_root / str(task_id) / "artifacts").resolve()

        file_payload = f"CONTENT_FOR_{attack_filename}_{uuid.uuid4().hex}".encode("utf-8")

        # Attempt path traversal upload
        upload_res = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": (attack_filename, file_payload, "application/octet-stream")},
        )
        assert upload_res.status_code == 201
        artifact = upload_res.json()

        # Check response metadata
        assert artifact["name"] == expected_sanitized_name
        assert artifact["size"] == len(file_payload)

        # Verify filesystem containment
        dest_path = Path(artifact["path"]).resolve()
        assert dest_path.is_file(), f"Expected uploaded file to exist at {dest_path}"
        assert dest_path.name == expected_sanitized_name

        # Strict containment verification: dest_path must be directly within expected_artifacts_dir
        assert dest_path.parent == expected_artifacts_dir, (
            f"Escaped directory! dest_path={dest_path}, expected_dir={expected_artifacts_dir}"
        )
        assert dest_path.is_relative_to(expected_artifacts_dir)
        assert dest_path.is_relative_to(artifact_root.resolve())

        # Verify content on disk matches exactly
        assert dest_path.read_bytes() == file_payload

        # Ensure outside system directories or parent dirs were NOT touched
        assert not (tmp_path / "passwd").exists()
        assert not (tmp_path / "shadow").exists()
        assert not (tmp_path / "pwned.txt").exists()


def test_adversarial_large_file_upload_5mb(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Upload a 5 MB binary file; verify success, SHA256 integrity, size, and cleanup."""
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "5MB Upload Target"}).json()
        task_id = task["id"]

        target_size = 5 * 1024 * 1024  # exactly 5,242,880 bytes
        payload_bytes = os.urandom(target_size)
        expected_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        upload_res = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("large_5mb.bin", payload_bytes, "application/octet-stream")},
        )
        assert upload_res.status_code == 201
        artifact_data = upload_res.json()

        assert artifact_data["name"] == "large_5mb.bin"
        assert artifact_data["size"] == target_size
        assert artifact_data["content_type"] == "application/octet-stream"

        dest_file = Path(artifact_data["path"])
        assert dest_file.is_file()
        assert dest_file.stat().st_size == target_size

        # Verify SHA256 checksum on disk
        actual_sha256 = hashlib.sha256(dest_file.read_bytes()).hexdigest()
        assert actual_sha256 == expected_sha256

        # Verify no temporary files remain in artifacts dir
        artifacts_dir = dest_file.parent
        tmp_files = list(artifacts_dir.glob("*.tmp*"))
        assert tmp_files == [], f"Lingering temporary files found: {tmp_files}"

        # Verify task metadata reflects the 5 MB attachment
        task_record = client.get(f"/tasks/{task_id}").json()
        assert len(task_record["attachments"]) == 1
        assert task_record["attachments"][0]["name"] == "large_5mb.bin"
        assert task_record["attachments"][0]["size"] == target_size


def test_adversarial_empty_file_upload(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Upload a 0-byte file; verify upload succeeds and records 0 size."""
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "0-Byte Upload Target"}).json()
        task_id = task["id"]

        empty_bytes = b""
        upload_res = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("empty.dat", empty_bytes, "application/octet-stream")},
        )
        assert upload_res.status_code == 201
        artifact_data = upload_res.json()

        assert artifact_data["name"] == "empty.dat"
        assert artifact_data["size"] == 0

        dest_file = Path(artifact_data["path"])
        assert dest_file.is_file()
        assert dest_file.stat().st_size == 0
        assert dest_file.read_bytes() == b""

        # Verify task metadata reflects size 0
        task_record = client.get(f"/tasks/{task_id}").json()
        assert len(task_record["attachments"]) == 1
        assert task_record["attachments"][0]["name"] == "empty.dat"
        assert task_record["attachments"][0]["size"] == 0


def test_adversarial_duplicate_upload_atomic_replacement_and_deduplication(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Upload data.bin (size A), then upload data.bin again (size B).

    Verify:
    1. Disk file is atomically replaced with size B content.
    2. Task metadata reflects size B.
    3. No duplicate attachment entries in task.meta['attachments'].
    4. Overwrite succeeds even if previous file had 0o444 permissions.
    """
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Duplicate Overwrite Target"}).json()
        task_id = task["id"]

        # Phase 1: Upload size A
        size_a = 16384  # 16 KiB
        payload_a = os.urandom(size_a)
        sha_a = hashlib.sha256(payload_a).hexdigest()

        res1 = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("data.bin", payload_a, "application/octet-stream")},
        )
        assert res1.status_code == 201
        data1 = res1.json()
        assert data1["name"] == "data.bin"
        assert data1["size"] == size_a

        dest_path = Path(data1["path"])
        assert dest_path.is_file()
        assert dest_path.stat().st_size == size_a
        assert hashlib.sha256(dest_path.read_bytes()).hexdigest() == sha_a

        # Adversarial stress: simulate worker making file read-only (chmod 0o444)
        dest_path.chmod(0o444)
        assert stat.S_IMODE(dest_path.stat().st_mode) == 0o444

        # Phase 2: Upload size B with same filename
        size_b = 65536  # 64 KiB
        payload_b = os.urandom(size_b)
        sha_b = hashlib.sha256(payload_b).hexdigest()

        res2 = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("data.bin", payload_b, "application/octet-stream")},
        )
        assert res2.status_code == 201
        data2 = res2.json()
        assert data2["name"] == "data.bin"
        assert data2["size"] == size_b

        # Verify disk file replaced
        assert dest_path.is_file()
        assert dest_path.stat().st_size == size_b
        assert hashlib.sha256(dest_path.read_bytes()).hexdigest() == sha_b

        # Verify metadata has exactly 1 entry and reflects size B
        task_record = client.get(f"/tasks/{task_id}").json()
        attachments = task_record["attachments"]
        assert len(attachments) == 1, f"Expected exactly 1 attachment record, got: {attachments}"
        assert attachments[0]["name"] == "data.bin"
        assert attachments[0]["size"] == size_b
        assert attachments[0]["path"] == str(dest_path)


def test_adversarial_upload_non_existent_task_returns_404(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Verify uploading to a non-existent task returns 404 Not Found without creating files."""
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        # 1. Non-existent UUID
        fake_uuid = str(uuid.uuid4())
        res_uuid = client.post(
            f"/tasks/{fake_uuid}/attachments",
            files={"file": ("file.txt", b"payload", "text/plain")},
        )
        assert res_uuid.status_code == 404
        assert res_uuid.json() == {"detail": "task not found"}

        # 2. Non-existent integer task number
        res_num = client.post(
            "/tasks/999999/attachments",
            files={"file": ("file.txt", b"payload", "text/plain")},
        )
        assert res_num.status_code == 404
        assert res_num.json() == {"detail": "task not found"}

        # 3. Traversal-like task ID string
        res_trav = client.post(
            "/tasks/../../etc/attachments",
            files={"file": ("file.txt", b"payload", "text/plain")},
        )
        assert res_trav.status_code in (404, 307)

        # Verify no directories created in artifact_root
        if artifact_root.exists():
            assert list(artifact_root.iterdir()) == []


def test_adversarial_multiple_files_interleaved_deduplication(
    migrated_db: Path, tmp_path: Path
) -> None:
    """Upload multiple distinct files and update one in the middle; verify metadata integrity."""
    artifact_root = tmp_path / "artifacts"
    app = create_app(f"sqlite:///{migrated_db}", artifact_root=artifact_root)

    with TestClient(app) as client:
        task = client.post("/tasks", json={"title": "Multi-File Interleaved Target"}).json()
        task_id = task["id"]

        files_data = {
            "first.txt": b"first_content_1",
            "second.csv": b"a,b,c\n1,2,3\n",
            "third.json": b'{"key": "value"}',
        }

        # Upload initial files
        for fname, content in files_data.items():
            res = client.post(
                f"/tasks/{task_id}/attachments",
                files={"file": (fname, content, "text/plain")},
            )
            assert res.status_code == 201

        task_record = client.get(f"/tasks/{task_id}").json()
        assert len(task_record["attachments"]) == 3
        names = [a["name"] for a in task_record["attachments"]]
        assert names == ["first.txt", "second.csv", "third.json"]

        # Re-upload second.csv with updated content
        new_second_content = b"col1,col2\nx,y\n" * 50
        res_update = client.post(
            f"/tasks/{task_id}/attachments",
            files={"file": ("second.csv", new_second_content, "text/csv")},
        )
        assert res_update.status_code == 201

        updated_task = client.get(f"/tasks/{task_id}").json()
        atts = updated_task["attachments"]
        assert len(atts) == 3, f"Expected 3 attachments, got: {atts}"
        assert [a["name"] for a in atts] == ["first.txt", "second.csv", "third.json"]

        # Verify second.csv reflects new size and content type
        second_att = next(a for a in atts if a["name"] == "second.csv")
        assert second_att["size"] == len(new_second_content)
        assert second_att["content_type"] == "text/csv"
        assert Path(second_att["path"]).read_bytes() == new_second_content
