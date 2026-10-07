#!/usr/bin/env python3
"""스레드 자동 발행 도구 - 파이썬 기본 기능만 사용해요 (따로 설치할 게 없어요).

사용법
  python3 scripts/threads_tool.py post      # 승인된(READY.txt 있는) 글 중 첫 번째를 발행
  python3 scripts/threads_tool.py check     # 토큰이 잘 연결됐는지 확인
  python3 scripts/threads_tool.py refresh   # 토큰 갱신 (새 토큰을 NEW_TOKEN_FILE 에 저장)

환경변수
  THREADS_ACCESS_TOKEN  스레드 접근 토큰 (필수)
  DRY_RUN=true          실제로 올리지 않고 어떤 글이 올라갈지만 보여줘요
"""
import datetime
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = os.environ.get("THREADS_API_BASE", "https://graph.threads.net/v1.0").rstrip("/")
API_ROOT = os.environ.get("THREADS_API_ROOT", "https://graph.threads.net").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
POSTED = ROOT / "posted"
IMAGE_EXT = {".png", ".jpg", ".jpeg"}
MAX_TEXT = 500            # 스레드 글자 수 제한
MAX_IMAGES = 20           # 한 글에 넣을 수 있는 이미지 수
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT", "120"))
PUBLISH_DELAY = float(os.environ.get("PUBLISH_DELAY", "5"))
KST = datetime.timezone(datetime.timedelta(hours=9))


class ApiError(Exception):
    pass


def say(msg):
    print(msg, flush=True)


def token():
    tok = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not tok:
        sys.exit("THREADS_ACCESS_TOKEN 이 비어 있어요. 저장소 Settings > Secrets and variables > Actions 에 토큰을 넣었는지 확인해주세요.")
    return tok


def call(method, path, params=None, base=None, tok=None):
    """스레드 API 호출. 주소(토큰이 들어 있어요)는 절대 화면에 출력하지 않아요."""
    params = dict(params or {})
    params["access_token"] = tok or token()
    data = urllib.parse.urlencode(params)
    url = f"{(base or API).rstrip('/')}/{path.lstrip('/')}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data)
    else:
        req = urllib.request.Request(url, data=data.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise ApiError(f"스레드 서버가 거절했어요 (HTTP {e.code}): {body}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"스레드 서버에 연결하지 못했어요: {e.reason}") from None


def wait_ready(container_id):
    """스레드가 글(또는 이미지)을 처리 끝낼 때까지 기다려요."""
    deadline = time.time() + POLL_TIMEOUT
    while True:
        info = call("GET", container_id, {"fields": "status,error_message"})
        status = info.get("status")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise ApiError(f"처리에 실패했어요 ({status}): {info.get('error_message', '')}")
        if time.time() > deadline:
            raise ApiError("처리 시간이 너무 오래 걸려서 중단했어요.")
        time.sleep(POLL_INTERVAL)


def read_text(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path.name} 을(를) 읽을 수 없어요. 메모장에서 'UTF-8'로 저장해주세요.")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def folders():
    if not QUEUE.exists():
        return []
    return sorted(p for p in QUEUE.iterdir() if p.is_dir() and not p.name.startswith("."))


def is_ready(folder):
    return (folder / "READY.txt").exists() or (folder / "READY").exists()


def load_post(folder):
    text_file = folder / "text.txt"
    if not text_file.exists():
        raise ValueError(f"'{folder.name}' 폴더에 text.txt 가 없어요.")
    text = read_text(text_file)
    images = sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXT),
                    key=lambda f: f.name)
    if len(text) > MAX_TEXT:
        raise ValueError(f"'{folder.name}' 글이 {len(text)}자예요. 스레드는 {MAX_TEXT}자까지만 돼요. {len(text) - MAX_TEXT}자를 줄여주세요.")
    if not text and not images:
        raise ValueError(f"'{folder.name}' 폴더에 올릴 글도 이미지도 없어요.")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"'{folder.name}' 이미지가 {len(images)}장이에요. 최대 {MAX_IMAGES}장까지만 돼요.")
    for f in images:
        if f.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"'{f.name}' 이미지가 8MB보다 커요. 용량을 줄여주세요.")
    return text, images


def image_url(folder, image):
    base = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY")
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        if not repo:
            sys.exit("이미지 주소를 만들 수 없어요 (GITHUB_REPOSITORY 가 없어요). GitHub Actions 에서 실행해주세요.")
        base = f"https://raw.githubusercontent.com/{repo}/{branch}"
    rel = f"queue/{folder.name}/{image.name}"
    return f"{base}/{urllib.parse.quote(rel)}"


def publish(user_id, text, urls):
    if not urls:
        container = call("POST", f"{user_id}/threads", {"media_type": "TEXT", "text": text})["id"]
    elif len(urls) == 1:
        params = {"media_type": "IMAGE", "image_url": urls[0]}
        if text:
            params["text"] = text
        container = call("POST", f"{user_id}/threads", params)["id"]
    else:
        children = []
        for u in urls:
            child = call("POST", f"{user_id}/threads",
                         {"media_type": "IMAGE", "image_url": u, "is_carousel_item": "true"})["id"]
            children.append(child)
        for child in children:
            wait_ready(child)
        params = {"media_type": "CAROUSEL", "children": ",".join(children)}
        if text:
            params["text"] = text
        container = call("POST", f"{user_id}/threads", params)["id"]
    wait_ready(container)
    time.sleep(PUBLISH_DELAY)
    return call("POST", f"{user_id}/threads_publish", {"creation_id": container})["id"]


def cmd_check():
    me = call("GET", "me", {"fields": "id,username"})
    say(f"연결 성공: @{me.get('username')} (id {me.get('id')})")
    return 0


def cmd_post():
    dry = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    all_folders = folders()
    ready = [f for f in all_folders if is_ready(f)]
    waiting = [f for f in all_folders if not is_ready(f)]
    for f in waiting:
        say(f"승인 대기 (READY.txt 없음): {f.name}")
    if not ready:
        say("발행할 승인된 글이 없어요. (queue 폴더 안에 READY.txt 가 있는 글만 올라가요)")
        return 0

    folder = ready[0]
    text, images = load_post(folder)
    say(f"다음에 올릴 글: {folder.name} (남은 승인된 글 {len(ready)}개)")
    say(f"글자 수 {len(text)}자, 이미지 {len(images)}장")
    urls = [image_url(folder, f) for f in images]

    if dry:
        say("--- 연습 실행: 실제로 올리지 않았어요 ---")
        say(text if text else "(본문 없음)")
        for u in urls:
            say(f"이미지: {u}")
        return 0

    user_id = os.environ.get("THREADS_USER_ID") or call("GET", "me", {"fields": "id"})["id"]
    media_id = publish(user_id, text, urls)
    say(f"발행 완료! (글 id {media_id})")

    permalink = ""
    try:
        permalink = call("GET", media_id, {"fields": "permalink"}).get("permalink", "")
    except ApiError:
        pass
    if permalink:
        say(f"글 주소: {permalink}")

    POSTED.mkdir(exist_ok=True)
    dest = POSTED / folder.name
    if dest.exists():
        dest = POSTED / f"{folder.name}-{datetime.datetime.now(KST):%Y%m%d%H%M%S}"
    shutil.move(str(folder), str(dest))
    (dest / "result.json").write_text(json.dumps({
        "media_id": media_id,
        "permalink": permalink,
        "posted_at": datetime.datetime.now(KST).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    say(f"'{folder.name}' 폴더를 posted 로 옮겼어요.")
    return 0


def cmd_refresh():
    """토큰을 60일짜리로 교환하거나 갱신해서 NEW_TOKEN_FILE 에 저장해요."""
    tok = token()
    secret = os.environ.get("THREADS_APP_SECRET", "").strip()
    result, how, errors = None, "", []
    if secret:
        try:
            result = call("GET", "access_token",
                          {"grant_type": "th_exchange_token", "client_secret": secret},
                          base=API_ROOT, tok=tok)
            how = "교환"
        except ApiError as e:
            errors.append(f"교환 실패: {e}")
    if result is None:
        try:
            result = call("GET", "refresh_access_token",
                          {"grant_type": "th_refresh_token"}, base=API_ROOT, tok=tok)
            how = "갱신"
        except ApiError as e:
            errors.append(f"갱신 실패: {e}")
    if result is None or "access_token" not in result:
        for e in errors:
            say(f"::error::{e}")
        return 1
    new = result["access_token"]
    say(f"::add-mask::{new}")
    out = os.environ.get("NEW_TOKEN_FILE")
    if not out:
        sys.exit("NEW_TOKEN_FILE 이 없어서 새 토큰을 저장할 수 없어요.")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(new)
    days = int(result.get("expires_in", 0)) // 86400
    say(f"토큰 {how} 완료: 앞으로 약 {days}일 동안 쓸 수 있어요.")
    return 0


COMMANDS = {"post": cmd_post, "check": cmd_check, "refresh": cmd_refresh}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    try:
        return COMMANDS[sys.argv[1]]() or 0
    except (ApiError, ValueError) as e:
        say(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
