import pytest
from app.services.downloader import downloader

@pytest.mark.asyncio
async def test_extract_info_invalid_url():
    result = await downloader.get_info_async("https://invalid-domain-test.com/xyz")
    assert result is None
