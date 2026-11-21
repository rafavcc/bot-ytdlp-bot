import sys, json
import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.impersonate import ImpersonateTarget

url = sys.argv[1].split("?")[0].rstrip("/")
imp = {"impersonate": ImpersonateTarget.from_str("firefox")}

with yt_dlp.YoutubeDL({"cookiesfrombrowser": ("brave",), "quiet": True}) as ydl:
    ydl.cookiejar.set_cookie(
        __import__("http.cookiejar").cookiejar.Cookie(
            0, "over18", "1", None, False, ".reddit.com", True, True,
            "/", True, False, None, False, None, None, {}))
    ydl.urlopen(Request("https://old.reddit.com/", extensions=imp)).read()
    r = ydl.urlopen(Request(url + "/.json", query={"raw_json": 1}, extensions=imp))
    d = json.load(r)[0]["data"]["children"][0]["data"]

urls = [m["s"].get("u") or m["s"].get("gif") or m["s"].get("mp4")
        for m in (d.get("media_metadata") or {}).values() if m.get("s")]
print("\n".join(urls or [d.get("url_overridden_by_dest")]))