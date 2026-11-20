from datetime import date

import pytest

from cic_mcp_lab.cic.oss import build_object_key, guess_image_type, public_url


def test_build_object_key_uses_date_folder_and_lowercase_suffix():
    key = build_object_key("Photo.PNG", date(2026, 11, 20), "abc123")
    assert key == "cic-mcp-lab/20261120/abc123.png"


def test_public_url_points_to_bucket_endpoint():
    url = public_url("my-bucket", "oss-cn-guangzhou.aliyuncs.com", "cic-mcp-lab/20261120/a b.png")
    assert url == "https://my-bucket.oss-cn-guangzhou.aliyuncs.com/cic-mcp-lab/20261120/a%20b.png"


def test_guess_image_type_accepts_images_and_rejects_other_files():
    assert guess_image_type("a.png") == "image/png"
    assert guess_image_type("product.webp") == "image/webp"
    with pytest.raises(ValueError, match="Not an image"):
        guess_image_type("report.pdf")
