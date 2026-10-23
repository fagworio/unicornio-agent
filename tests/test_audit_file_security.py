import json
import os
import stat

from unicornio_editor.cli import _write_audit


def test_audit_file_is_atomically_replaced_with_private_permissions(tmp_path):
    target = tmp_path / "work" / "batches" / "manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"old": true}', encoding="utf-8")
    os.chmod(target, 0o644)

    result = _write_audit(target, {"signed_url": "https://cdn.test/image?token=secret"})

    assert result == str(target)
    assert json.loads(target.read_text(encoding="utf-8"))["signed_url"].endswith("token=secret")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(path.name for path in target.parent.iterdir()) == ["manifest.json"]
