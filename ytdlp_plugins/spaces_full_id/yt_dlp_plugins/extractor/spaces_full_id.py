"""yt-dlp reads Space IDs as exactly 13 characters, which truncates X's newer 14-character IDs."""

from yt_dlp.extractor.twitter import TwitterSpacesIE


class TwitterSpacesFullIdIE(TwitterSpacesIE):
    # yt-dlp builds the title of an untitled Space from this name, and that title becomes the file name.
    IE_NAME = "X Space"
    _VALID_URL = TwitterSpacesIE._BASE_REGEX + r"i/spaces/(?P<id>[0-9a-zA-Z]{13,})"
