"""Index all permanent MeteoHub releases and still-recoverable Actions artifacts."""
import json
import os
import shutil
import subprocess
from pathlib import Path

CACHE = Path(os.environ.get("RAIN_CACHE", ".rain-cache"))
STATE = Path("data/rain-learning")
REPO = os.environ.get("GITHUB_REPOSITORY", "lcricc/enduro-weather-engine")


def api_pages(endpoint):
    pages = subprocess.check_output(["gh", "api", "--paginate", "--slurp",
                                     f"repos/{REPO}/{endpoint}?per_page=100"])
    for page in json.loads(pages):
        yield from (page["artifacts"] if isinstance(page, dict) else page)


def discover(releases, artifacts, previous):
    by_name = {}
    for artifact in artifacts:
        by_name.setdefault(artifact["name"], []).append(artifact)
    manifest = {}
    def add(key, kind, published, url, name, *, optional=False, partial=False):
        entry = manifest.get(key) or previous.get(key, {})
        when = min(published, entry.get("published", published))
        manifest[key] = dict(key=key, kind=kind, published=when, url=url,
                             name=name, optional=optional, partial=partial)
        if entry.get("empty"):
            manifest[key]["empty"] = True
    for r in releases:
        tag = r["tag_name"]
        legacy = tag.startswith("archive-")
        if not (legacy or tag.startswith(("meteohub-observations-", "meteohub-forecasts-"))):
            continue
        for asset in r["assets"]:
            name = asset["name"]
            if not name.endswith(".tar.gz"):
                continue
            kind = name.split("-")[0].split(".")[0]
            if kind not in ("observations", "forecasts"):
                continue
            key = asset["digest"].removeprefix("sha256:")
            peers = by_name.get(("enduro-" if kind=="forecasts" else "")+tag, [])
            # Archive workflow packages the source run. Use the matching artifact's
            # timestamp only when the source is unambiguous across run attempts.
            published = min(r["published_at"],peers[0]["created_at"]) if len(peers)==1 else r["published_at"]
            add(key,kind,published,asset["url"],name,optional=legacy or kind=="observations")
            if len(peers)==1:
                by_name.pop(peers[0]["name"])
    for name, items in by_name.items():
        if not name.startswith(("meteohub-observations-", "enduro-meteohub-forecasts-")):
            continue
        for a in items:
            key = (a.get("digest") or "").removeprefix("sha256:")
            if a["expired"] or not key:
                continue
            kind = "forecasts" if name.startswith("enduro-") else "observations"
            add(key,kind,a["created_at"],a["archive_download_url"],f"artifact-{a['id']}.zip",
                optional=True,partial=True)
    # Keep previously seen inputs even if their Actions retention expires.
    for key, old in previous.items():
        manifest.setdefault(key,old)
    return manifest


def prepare():
    CACHE.mkdir(exist_ok=True, parents=True)
    STATE.mkdir(exist_ok=True, parents=True)
    index = STATE/"sources.json"
    previous = json.loads(index.read_text()) if index.exists() else {}
    sources = discover(api_pages("releases"),api_pages("actions/artifacts"),previous)
    manifest = []
    for key,source in sorted(sources.items()):
        if len(key)!=64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError(f"Invalid archive digest for {source['name']}")
        if source.get("empty"):
            continue
        path = CACHE/source["name"]
        cached = CACHE/(key+".parquet")
        durable = STATE/(key+".parquet")
        if durable.exists() and not cached.exists():
            shutil.copyfile(durable,cached)
        manifest.append({**source,"path":str(path)})
    manifest_path = CACHE/"manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    subprocess.run(["python","calibrate_rain.py",str(manifest_path),"--cache",str(CACHE)],check=True)
    for entry in manifest:
        key = entry["key"]
        projection = CACHE/(key+".parquet")
        if not projection.exists():
            sources[key]["empty"] = True
        elif entry["partial"] and not (STATE/projection.name).exists():
            shutil.copyfile(projection,STATE/projection.name)
        Path(entry["path"]).unlink(missing_ok=True)
    index.write_text(json.dumps(sources,sort_keys=True,indent=2)+"\n")


if __name__=="__main__":
    prepare()
