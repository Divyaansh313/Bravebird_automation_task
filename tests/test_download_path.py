import pytest

from app.errors import FinalInputError
from app.flows.github import safe_download_path


@pytest.mark.parametrize("suggested, expected", [
    ("statement-v1.0.0.pdf", "statement-v1.0.0.pdf"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\evil.exe", "evil.exe"),
    ("/abs/path/report.pdf", "report.pdf"),
    ("we$ird|name?.pdf", "we_ird_name_.pdf"),
    ("", "fallback.pdf"),
    ("..", "fallback.pdf"),
    (None, "fallback.pdf"),
])
def test_download_names_stay_inside_downloads_dir(tmp_path, suggested, expected):
    dest = safe_download_path(tmp_path, suggested, fallback="fallback.pdf")
    assert dest.parent == tmp_path.resolve()
    assert dest.name == expected


def test_symlinked_escape_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "trap.pdf").symlink_to(outside / "trap.pdf")
    with pytest.raises(FinalInputError):
        safe_download_path(downloads, "trap.pdf", fallback="x.pdf")
