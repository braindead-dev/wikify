"""Face recognition over image attachments — cluster the people who appear in
photos, ask the owner who they are, and enrich captions with the names.

    atlas faces <slug>      detect + cluster faces, save sample crops, add
                            "Who is face-01?" questions to wiki/questions.json
    (owner answers with a name)
    atlas faces <slug>      applies answers: captions of images showing that
                            face gain "pictured: <name>" — extraction picks the
                            change up through normal hash invalidation

State is global (attachments are shared across wikis): embeddings in
`chats/_faces.npz`, clusters + names in `chats/_faces.json`, sample crops under
`chats/_faces/<face-id>/`. Requires the optional deps: `pip install
'imessage-analysis[faces]'`. Everything runs locally.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from imessage import MessagesDB

from .caption import CACHE as CAPTION_CACHE
from .caption import _PROMPT_VERSION, load_captions
from .config import FaceConfig
from .store import _atomic_write

FACES_DIR = Path("chats/_faces")
STATE = Path("chats/_faces.json")
EMBED = Path("chats/_faces.npz")


def _deps():
    try:
        import cv2
        import numpy
        from insightface.app import FaceAnalysis
        return cv2, numpy, FaceAnalysis
    except ImportError as e:
        raise SystemExit(f"faces needs optional deps ({e.name}) — "
                         "pip install insightface onnxruntime opencv-python-headless")


def _load_image(cv2, path):
    img = cv2.imread(path)
    if img is None:                                   # HEIC etc. — normalize via sips
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            out = tmp.name
        try:
            subprocess.run(["sips", "-s", "format", "jpeg", path, "--out", out],
                           capture_output=True, timeout=30)
            img = cv2.imread(out)
        finally:
            Path(out).unlink(missing_ok=True)
    return img


_LVFACE_URL = ("https://huggingface.co/bytedance-research/LVFace/resolve/main/"
               "LVFace-{s}_Glint360K/LVFace-{s}_Glint360K.onnx")


def _lvface_session(name):
    """Download (once) and load an LVFace ONNX embedder — a stronger drop-in for
    the recognition stage; insightface still does detection + alignment."""
    import urllib.request
    import onnxruntime
    size = name.split("-")[1].upper()
    path = Path.home() / ".insightface" / "models" / f"LVFace-{size}_Glint360K.onnx"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_LVFACE_URL.format(s=size), path)
    return onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def _embedder(cfg, app):
    """Returns f(img, face) -> normed 512-d embedding, per cfg.embed."""
    _, np, _ = _deps()
    if cfg.embed == "buffalo_l":
        return lambda img, f: f.normed_embedding
    from insightface.utils import face_align
    sess = _lvface_session(cfg.embed)
    inp = sess.get_inputs()[0].name

    def embed(img, f):
        crop = face_align.norm_crop(img, landmark=f.kps)        # 112x112 aligned
        x = ((crop[:, :, ::-1].astype("float32") - 127.5) / 127.5)  # BGR→RGB, arcface norm
        e = sess.run(None, {inp: x.transpose(2, 0, 1)[None]})[0][0]
        return e / (np.linalg.norm(e) or 1.0)
    return embed


def _detect(chat_ids, cfg, verbose) -> None:
    """Detect + embed faces in every captioned image; incremental via EMBED.
    Switching embedding models rescans from scratch (embeddings don't mix)."""
    cv2, np, FaceAnalysis = _deps()
    paths = sorted(load_captions())
    done_paths, rows = set(), []
    if EMBED.exists():
        data = np.load(EMBED, allow_pickle=True)
        prior = str(data["embed_model"]) if "embed_model" in data.files else "buffalo_l"
        if prior == cfg.embed:
            rows = list(zip(data["paths"], data["boxes"], data["scores"], data["embeddings"]))
            done_paths = set(data["scanned"])
        elif verbose:
            print(f"[faces] embed model changed → rescanning with {cfg.embed}", flush=True)
    todo = [p for p in paths if p not in done_paths and Path(p).exists()]
    if verbose:
        print(f"[faces] {len(done_paths)} scanned · {len(todo)} to scan · {cfg.embed}", flush=True)
    if not todo:
        return
    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    embed = _embedder(cfg, app)
    t0 = time.time()
    for i, path in enumerate(todo, 1):
        img = _load_image(cv2, path)
        if img is not None:
            for f in app.get(img):
                if f.det_score >= cfg.min_det:
                    rows.append((path, f.bbox.astype(int), float(f.det_score), embed(img, f)))
        done_paths.add(path)
        if verbose and i % 50 == 0:
            print(f"\r  [faces] {i}/{len(todo)} images · {len(rows)} faces "
                  f"· {(time.time() - t0) / i:.2f}s/img", end="", flush=True)
            _save_embed(np, rows, done_paths, cfg.embed)
    _save_embed(np, rows, done_paths, cfg.embed)
    if verbose:
        print(f"\n[faces] {len(rows)} faces across {len(done_paths)} images", flush=True)


def _save_embed(np, rows, scanned, embed_model) -> None:
    np.savez_compressed(
        EMBED, paths=np.array([r[0] for r in rows], dtype=object),
        boxes=np.array([r[1] for r in rows]) if rows else np.zeros((0, 4)),
        scores=np.array([r[2] for r in rows]),
        embeddings=np.array([r[3] for r in rows]) if rows else np.zeros((0, 512)),
        scanned=np.array(sorted(scanned), dtype=object),
        embed_model=embed_model)


def _cluster(cfg, verbose) -> dict:
    """Greedy centroid clustering of embeddings (cosine, quality-first)."""
    _, np, _ = _deps()
    data = np.load(EMBED, allow_pickle=True)
    order = np.argsort(-data["scores"])
    centroids, members = [], []
    for idx in order:
        e = data["embeddings"][idx]
        if centroids:
            sims = np.array(centroids) @ e
            best = int(np.argmax(sims))
            if sims[best] >= cfg.threshold:
                members[best].append(int(idx))
                n = len(members[best])
                centroids[best] = (np.array(centroids[best]) * (n - 1) + e) / n
                centroids[best] /= np.linalg.norm(centroids[best])
                continue
        centroids.append(e)
        members.append([int(idx)])
    clusters = sorted((m for m in members if len(m) >= cfg.min_cluster), key=len, reverse=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {"faces": {}}
    # names survive re-clustering: a new cluster inherits the name of the old
    # cluster it most overlaps with (by image paths)
    old_named = [(f["name"], set(f["paths"])) for f in state["faces"].values() if f.get("name")]
    state["faces"] = {}
    for n, m in enumerate(clusters, 1):
        paths = sorted({str(data["paths"][i]) for i in m})
        name = next((nm for nm, old in old_named
                     if len(old & set(paths)) >= max(1, len(old) // 2)), None)
        state["faces"][f"face-{n:02d}"] = {
            "count": len(m), "paths": paths,
            "best": [int(i) for i in m[:cfg.crops]], "name": name,
        }
    if verbose:
        named = sum(1 for f in state["faces"].values() if f["name"])
        print(f"[faces] {len(clusters)} people found (≥{cfg.min_cluster} appearances) "
              f"· {named} already named", flush=True)
    _atomic_write(STATE, state)
    return state


def _save_crops(state, cfg, verbose) -> None:
    cv2, np, _ = _deps()
    data = np.load(EMBED, allow_pickle=True)
    for fid, f in state["faces"].items():
        out_dir = FACES_DIR / fid
        out_dir.mkdir(parents=True, exist_ok=True)
        for j, idx in enumerate(f["best"], 1):
            img = _load_image(cv2, str(data["paths"][idx]))
            if img is None:
                continue
            x1, y1, x2, y2 = data["boxes"][idx]
            pad = int(0.4 * (y2 - y1))
            crop = img[max(y1 - pad, 0):y2 + pad, max(x1 - pad, 0):x2 + pad]
            if crop.size:
                cv2.imwrite(str(out_dir / f"{j}.jpg"), crop)


def _sync_questions(state, wiki_dir: Path, verbose) -> None:
    """Unnamed clusters become questions; answered questions become names."""
    path = wiki_dir / "questions.json"
    questions = json.loads(path.read_text()) if path.exists() else []
    named = 0
    for q in questions:
        if q.get("kind") == "face" and not q.get("applied"):
            answer = str(q.get("answer", "")).strip()
            if answer and q["face_id"] in state["faces"]:
                state["faces"][q["face_id"]]["name"] = None if answer.lower() == "no" else answer
                q["applied"] = True
                named += 1
    asked = {q.get("face_id") for q in questions if q.get("kind") == "face"}
    added = 0
    for fid, f in state["faces"].items():
        if fid not in asked and not f.get("name"):
            questions.append({
                "question": f"Who is {fid}? ({f['count']} appearances — "
                            f"sample crops in {FACES_DIR / fid}/)",
                "kind": "face", "face_id": fid, "evidence": "", "answer": ""})
            added += 1
    _atomic_write(path, questions)
    _atomic_write(STATE, state)
    if verbose:
        print(f"[faces] {named} names applied · {added} new questions → {path}", flush=True)


def _enrich_captions(state, verbose) -> None:
    """Append 'pictured: <names>' to captions of images whose faces are named.
    Idempotent: rebuilt from the base caption each time. Extraction sees the
    change through normal chunk-hash invalidation."""
    if not CAPTION_CACHE.exists():
        return
    data = json.loads(CAPTION_CACHE.read_text())
    captions = data.get("captions", {})
    base = data.get("base", dict(captions))          # unenriched originals
    by_path = {}
    for f in state["faces"].values():
        if f.get("name"):
            for p in f["paths"]:
                by_path.setdefault(p, []).append(f["name"])
    enriched = 0
    for p, caption in base.items():
        names = sorted(set(by_path.get(p, [])))
        captions[p] = caption + (f" (pictured: {', '.join(names)})" if names else "")
        enriched += bool(names)
    _atomic_write(CAPTION_CACHE, {"version": _PROMPT_VERSION, "captions": captions, "base": base})
    if verbose and enriched:
        print(f"[faces] {enriched} captions enriched with names — "
              "run `atlas extract` + `atlas wiki` to fold in", flush=True)


def build_faces(chat_ids, wiki_dir, config: FaceConfig = None, verbose=True) -> None:
    cfg = config or FaceConfig()
    _detect(chat_ids, cfg, verbose)
    state = _cluster(cfg, verbose)
    _save_crops(state, cfg, verbose)
    _sync_questions(state, Path(wiki_dir), verbose)
    _enrich_captions(state, verbose)
