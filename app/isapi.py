import uuid
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

    def get_input_proxy_channels(self) -> list[dict]:
        """IP-camera slots (ONVIF-proxied through the DVR, channels 9/10 on
        this unit) aren't listed by get_video_input_channels (analog-only)
        — they have their own discovery endpoint."""
        data = self._get_xml("/ISAPI/ContentMgmt/InputProxy/channels")
        return _as_list(data["InputProxyChannelList"]["InputProxyChannel"])

    def get_snapshot(self, stream_id: int) -> bytes:
        resp = self._client.get(f"/ISAPI/Streaming/channels/{stream_id}/picture")
        resp.raise_for_status()
        return resp.content

    def get_ptz_capabilities(self, channel_id: int) -> dict | None:
        try:
            data = self._get_xml(f"/ISAPI/PTZCtrl/channels/{channel_id}/capabilities")
            return data["PTZChannelCap"]
        except httpx.HTTPStatusError as exc:
            # 404: no PTZ endpoint for this channel at all. 400/badXmlContent:
            # IP-proxy channels (9/10 on this DVR) reject this specific
            # endpoint with an unhelpful error even though the DVR does
            # proxy real PTZ *control* for them (confirmed by physically
            # observing a continuous-move command turn the camera — see
            # ARCHITECTURE.md "PTZ for IP-proxy channels"). Treated as "no
            # PTZ" here since this method only reports capabilities.
            if exc.response.status_code in (404, 400):
                return None
            raise

    def ptz_continuous_move(self, channel_id: int, pan: int, tilt: int, zoom: int) -> None:
        """Speeds are -100..100 (ISAPI convention). Confirmed empirically
        (physical camera observation, not just the DVR's response) that
        positive pan turns the camera right, for a proxied ONVIF channel —
        see ARCHITECTURE.md. Tilt/zoom sign follow the same documented
        ISAPI convention but haven't been independently verified."""
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<PTZData>
<pan>{pan}</pan>
<tilt>{tilt}</tilt>
<zoom>{zoom}</zoom>
</PTZData>"""
        resp = self._client.put(
            f"/ISAPI/PTZCtrl/channels/{channel_id}/continuous",
            content=body,
            headers={"Content-Type": "application/xml"},
        )
        resp.raise_for_status()

    def ptz_stop(self, channel_id: int) -> None:
        self.ptz_continuous_move(channel_id, 0, 0, 0)

    def _search_recordings_page(self, search_id: str, track_id: int, start_time: str, end_time: str, max_results: int) -> dict:
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

    def search_recordings(self, track_id: int, start_time: str, end_time: str, max_pages: int = 10, page_size: int = 40) -> list[dict]:
        """Search recorded segments for a track over a time range.

        The DVR paginates results and, when re-issuing the identical
        request with the *same* searchID, advances to the next page
        automatically (server tracks position per searchID) — confirmed
        during Phase 0 recon. Loops until responseStatusStrg != "MORE" or
        max_pages is hit, as a safety cap against unbounded loops.
        """
        search_id = str(uuid.uuid4())
        matches: list[dict] = []
        for _ in range(max_pages):
            result = self._search_recordings_page(search_id, track_id, start_time, end_time, page_size)
            for item in _as_list(result.get("matchList", {}).get("searchMatchItem")):
                matches.append({
                    "startTime": item["timeSpan"]["startTime"],
                    "endTime": item["timeSpan"]["endTime"],
                    "playbackURI": item["mediaSegmentDescriptor"]["playbackURI"],
                    "codecType": item["mediaSegmentDescriptor"]["codecType"],
                })
            if result.get("responseStatusStrg") != "MORE":
                break
        return matches
