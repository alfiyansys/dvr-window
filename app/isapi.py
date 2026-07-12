from typing import Any

import httpx
import xmltodict

from app.config import DeviceConfig


def _as_list(value: Any) -> list:
    """xmltodict returns a dict for a single repeated element, a list for
    more than one — normalize to always-a-list so callers don't special-case."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class ISAPIClient:
    def __init__(self, device: DeviceConfig):
        self._device = device
        self._client = httpx.Client(
            base_url=device.base_url,
            auth=httpx.DigestAuth(device.username, device.password),
            timeout=10.0,
        )

    def close(self) -> None:
        self._client.close()

    def _get_xml(self, path: str) -> dict:
        resp = self._client.get(path)
        resp.raise_for_status()
        return xmltodict.parse(resp.text)

    def _post_xml(self, path: str, body: str) -> dict:
        resp = self._client.post(path, content=body, headers={"Content-Type": "application/xml"})
        resp.raise_for_status()
        return xmltodict.parse(resp.text)

    def get_device_info(self) -> dict:
        return self._get_xml("/ISAPI/System/deviceInfo")["DeviceInfo"]

    def get_video_input_channels(self) -> list[dict]:
        data = self._get_xml("/ISAPI/System/Video/inputs/channels")
        return _as_list(data["VideoInputChannelList"]["VideoInputChannel"])

    def get_streaming_channels(self) -> list[dict]:
        data = self._get_xml("/ISAPI/Streaming/channels")
        return _as_list(data["StreamingChannelList"]["StreamingChannel"])

    def get_ptz_capabilities(self, channel_id: int) -> dict | None:
        try:
            data = self._get_xml(f"/ISAPI/PTZCtrl/channels/{channel_id}/capabilities")
            return data["PTZChannelCap"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def search_recordings(self, search_id: str, track_id: int, start_time: str, end_time: str, max_results: int = 40) -> dict:
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
<searchID>{search_id}</searchID>
<trackList>
<trackID>{track_id}</trackID>
</trackList>
<timeSpanList>
<timeSpan>
<startTime>{start_time}</startTime>
<endTime>{end_time}</endTime>
</timeSpan>
</timeSpanList>
<maxResults>{max_results}</maxResults>
<searchResultPostion>0</searchResultPostion>
<metadataList>
<metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
</metadataList>
</CMSearchDescription>"""
        return self._post_xml("/ISAPI/ContentMgmt/search", body)["CMSearchResult"]
