"""Сборка ролика по монтажному листу edl.json: готовый mp4 и тот же монтаж черновиком CapCut.

python3 build.py <папка ролика> [--no-capcut] [--preview]

В папке ролика должны лежать words.json (из transcribe.py) и edl.json (его пишет Claude). Формат edl.json
описан в SKILL.md. Всё, что видно в mp4, попадает и в CapCut: те же склейки, ускорение, наезды, картинки,
субтитры, звуки и музыка, поэтому в CapCut можно поправить любую мелочь руками.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "..", "fonts")
W, H, FPS = 1080, 1920, 30
PAD_BEFORE, PAD_AFTER = 0.10, 0.18   # запас до первого и после последнего слова куска, чтобы не съесть звук
MAX_GAP = 0.45                       # паузы длиннее этого внутри оставленного куска вырезаем
ZOOMS = {"punch": (1.8, 1.8), "push": (1.05, 1.45), "hold": (1.3, 1.3)}
TAIL = {"punch": 0.6, "push": 0.2, "hold": 0.3}
PIC_BOX = 760                        # картинка вписывается в квадрат 760×760
PIC_Y = {"bottom": 1420, "middle": 960, "top": 520}   # центр картинки по высоте кадра
SUB_Y = {"top": 210, "middle": 960, "bottom": 1500}   # центр строки субтитров


def probe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                          "-of", "json", path], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": float(d["format"]["duration"]), "width": v.get("width"), "height": v.get("height"),
            "audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


# ───────────── монтажный лист → время ─────────────

def segments_from(words: list[dict], keep: list, duration: float) -> list[list]:
    """Куски исходника [начало, конец] в секундах: оставленные слова плюс запас, длинные паузы вырезаны."""
    segs = []
    for a, b in keep:
        run = [w for w in words if a <= w["i"] <= b]
        if not run:
            continue
        start = run[0]
        for prev, w in zip(run, run[1:] + [None]):
            if w is None or w["start"] - prev["end"] > MAX_GAP:
                segs.append([max(0.0, start["start"] - PAD_BEFORE), min(duration, prev["end"] + PAD_AFTER)])
                start = w
    segs.sort()
    merged = []
    for s, e in segs:
        if merged and s <= merged[-1][1] + 0.02:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [[round(s, 3), round(e, 3)] for s, e in merged if e - s > 0.08]


class Timeline:
    """Перевод времени исходника во время готового ролика."""

    def __init__(self, segments, speed):
        self.segments, self.speed = segments, speed
        self.starts, t = [], 0.0
        for s, e in segments:
            self.starts.append(round(t, 3))
            t += (e - s) / speed
        self.total = round(t, 3)

    def out(self, t_src: float):
        for (s, e), o in zip(self.segments, self.starts):
            if s - 1e-3 <= t_src <= e + 1e-3:
                return round(o + (min(max(t_src, s), e) - s) / self.speed, 3)
        return None

    def seg_at(self, t_out: float) -> int:
        k = 0
        for i, o in enumerate(self.starts):
            if o <= t_out + 1e-6:
                k = i
        return k


def kept_words(words, tl: Timeline, fixes: dict):
    res = []
    for w in words:
        a, b = tl.out(w["start"]), tl.out(w["end"])
        if a is None:
            continue
        if b is None or b < a:
            b = a + max(0.08, (w["end"] - w["start"]) / tl.speed)
        res.append(dict(w, word=fixes.get(str(w["i"]), w["word"]), t0=a, t1=b))
    return res


def sub_chunks(kw, per: int, case: str):
    """Субтитры по 1–3 слова: новая строка после точки, паузы или каждые per слов."""
    chunks, cur = [], []
    for k, w in enumerate(kw):
        cur.append(w)
        nxt = kw[k + 1] if k + 1 < len(kw) else None
        if len(cur) >= per or re.search(r"[.!?…,:;]$", w["word"]) or nxt is None or nxt["t0"] - w["t1"] > 0.35:
            chunks.append(cur)
            cur = []
    res = []
    for k, c in enumerate(chunks):
        text = " ".join(re.sub(r"[.,!?…:;]+$", "", w["word"]) for w in c).strip()
        text = text.lower() if case == "lower" else text.upper() if case == "upper" else text
        end = chunks[k + 1][0]["t0"] if k + 1 < len(chunks) else c[-1]["t1"] + 0.2
        end = min(end, c[-1]["t1"] + 0.6)
        if text:
            res.append({"text": text, "t0": c[0]["t0"], "t1": max(end, c[0]["t0"] + 0.25)})
    return res


def zoom_spans(edl, by_i, total):
    spans = []
    for z in edl.get("zooms") or []:
        a, b = by_i.get(z["from"]), by_i.get(z["to"], by_i.get(z["from"]))
        if not a or not b:
            continue
        kind = z.get("kind", "punch")
        z0, z1 = ZOOMS.get(kind, ZOOMS["punch"])
        if z.get("scale"):
            z0 = z1 = float(z["scale"])
        t0, t1 = max(0.0, a["t0"] - 0.05), min(total, b["t1"] + TAIL.get(kind, 0.3))
        if t1 - t0 > 0.2:
            spans.append({"t0": round(t0, 3), "t1": round(t1, 3), "z0": z0, "z1": z1, "kind": kind})
    spans.sort(key=lambda s: s["t0"])
    for p, q in zip(spans, spans[1:]):   # наезды не накладываются
        p["t1"] = min(p["t1"], q["t0"] - 0.04)
    return [s for s in spans if s["t1"] - s["t0"] > 0.2]


def picture_items(edl, by_i, total, workdir):
    pics = []
    for p in edl.get("pictures") or []:
        w = by_i.get(p["word"])
        if not w or not os.path.exists(p["file"]):
            print(f"! картинка пропущена: {p.get('file')} (нет файла или слово вырезано)")
            continue
        pics.append({"t0": w["t0"], "secs": float(p.get("secs") or 3.0), "file": p["file"], "pos": p.get("pos", "bottom")})
    pics.sort(key=lambda p: p["t0"])
    for k, p in enumerate(pics):
        nxt = pics[k + 1]["t0"] if k + 1 < len(pics) else total
        p["t1"] = round(min(p["t0"] + p["secs"], nxt - 0.05, total), 3)
        img = Image.open(p["file"]).convert("RGBA")
        f = min(PIC_BOX / img.width, PIC_BOX / img.height)
        img = img.resize((max(2, int(img.width * f)) // 2 * 2, max(2, int(img.height * f)) // 2 * 2), Image.LANCZOS)
        p["png"] = os.path.join(workdir, f"pic{k}.png")
        img.save(p["png"])
        p["w"], p["h"] = img.size
    return [p for p in pics if p["t1"] - p["t0"] > 0.4]


def sound_items(edl, by_i):
    res = []
    for s in edl.get("sounds") or []:
        w = by_i.get(s["word"])
        if w and os.path.exists(s["file"]):
            res.append({"t0": w["t0"], "file": s["file"], "vol": float(s.get("vol") or 0.6)})
        else:
            print(f"! звук пропущен: {s.get('file')}")
    return res


def bw_spans(edl, by_i):
    res = []
    for b in edl.get("bw") or []:
        a, z = by_i.get(b["from"]), by_i.get(b["to"], by_i.get(b["from"]))
        if a and z:
            res.append((a["t0"], z["t1"] + 0.3))
    return res


# ───────────── готовый mp4 ─────────────

def _ass_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def write_ass(path, subs, st):
    def ts(t):
        cs = int(round(t * 100))
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"
    y = SUB_Y.get(st.get("pos", "top"), 210)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: S,{st.get('font', 'Jost')},{int(st.get('size', 72))},{_ass_color(st.get('color', '#FB7D7E'))},&H00FFFFFF,"
        f"&H00000000,&H64000000,{-1 if st.get('bold', True) else 0},0,0,0,100,100,0,0,1,{st.get('outline', 0)},{st.get('shadow', 1)},5,60,60,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for s in subs:
        text = s["text"].replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ts(s['t0'])},{ts(s['t1'])},S,,0,0,0,,{{\\pos({W // 2},{y})}}{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def zoom_expr(spans):
    """Выражение для zoompan: 1 вне наездов, внутри — постоянный или плавно растущий масштаб."""
    parts = []
    for s in spans:
        d = s["t1"] - s["t0"]
        z = f"{s['z0']}" if s["z0"] == s["z1"] else f"({s['z0']}+{s['z1'] - s['z0']:.3f}*(T-{s['t0']})/{d:.3f})"
        parts.append(f"between(T,{s['t0']},{s['t1']})*({z}-1)")
    expr = "1+" + "+".join(parts) if parts else "1"
    return expr.replace("T", f"(on/{FPS})")


def render_mp4(src, info, tl, subs, spans, pics, sounds, bws, edl, workdir, out, preview=False):
    speed = tl.speed
    ass = os.path.join(workdir, "subs.ass")
    write_ass(ass, subs, edl.get("subs") or {})
    inputs = ["-i", src]
    for p in pics:
        inputs += ["-loop", "1", "-t", f"{p['t1'] - p['t0']:.3f}", "-i", p["png"]]
    n = 1 + len(pics)
    music = edl.get("music") or {}
    music_idx = None
    if music.get("file") and os.path.exists(music["file"]):
        music_idx = n
        inputs += ["-stream_loop", "-1", "-i", music["file"]]
        n += 1
    snd_idx = []
    for s in sounds:
        snd_idx.append(n)
        inputs += ["-i", s["file"]]
        n += 1

    fc = []
    for i, (s, e) in enumerate(tl.segments):
        fc.append(f"[0:v]trim={s}:{e},setpts=PTS-STARTPTS[v{i}]")
        if info["audio"]:
            d = e - s
            fc.append(f"[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS,afade=t=in:d=0.012,"
                      f"afade=t=out:st={max(0, d - 0.025):.3f}:d=0.025[a{i}]")
    k = len(tl.segments)
    if info["audio"]:
        fc.append("".join(f"[v{i}][a{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[vc][ac]")
    else:
        fc.append("".join(f"[v{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=0[vc]")
    col = edl.get("color") or {}
    warm = float(col.get("warm", 0.03))
    grade = f"eq=contrast={float(col.get('contrast', 1.05))}:saturation={float(col.get('saturation', 1.0))}" \
            f":brightness={float(col.get('brightness', 0.0))},colorbalance=rm={warm:.3f}:bm={-warm * 0.6:.3f}"
    fc.append(f"[vc]setpts=PTS/{speed},fps={FPS},scale='trunc(iw*sar/2)*2':ih,setsar=1,"
              f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
              f"zoompan=z='{zoom_expr(spans)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
              f"{grade}[g0]")
    last = "g0"
    for j, (a, b) in enumerate(bws):
        fc.append(f"[{last}]hue=s=0:enable='between(t,{a:.3f},{b:.3f})'[bw{j}]")
        last = f"bw{j}"
    for i, p in enumerate(pics):
        y = PIC_Y.get(p["pos"], 1420) - p["h"] // 2
        fc.append(f"[{i + 1}:v]format=rgba,setpts=PTS-STARTPTS+{p['t0']:.3f}/TB[p{i}]")
        fc.append(f"[{last}][p{i}]overlay=x=(W-w)/2:y={y}:eof_action=pass:"
                  f"enable='between(t,{p['t0']:.3f},{p['t1']:.3f})'[o{i}]")
        last = f"o{i}"
    ass_esc = ass.replace("\\", "/").replace(":", "\\:")
    fonts_esc = os.path.abspath(FONTS).replace(":", "\\:")
    fc.append(f"[{last}]ass='{ass_esc}':fontsdir='{fonts_esc}',format=yuv420p[vout]")

    total = tl.total
    if info["audio"]:
        fc.append(f"[ac]atempo={speed},highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[voice]"
                  if speed != 1.0 else "[ac]highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[voice]")
    else:
        fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f}[voice]")
    mix = ["[voice]"]
    if music_idx is not None:
        mv = float(music.get("vol") or 0.15)
        fc.append(f"[{music_idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,aresample=48000,volume={mv:.3f},"
                  f"afade=t=out:st={max(0, total - 1.5):.3f}:d=1.5[mus]")
        mix.append("[mus]")
    for j, (s, idx) in enumerate(zip(sounds, snd_idx)):
        ms = int(s["t0"] * 1000)
        fc.append(f"[{idx}:a]aformat=channel_layouts=stereo,aresample=48000,atrim=end=4,"
                  f"volume={s['vol']:.2f},adelay={ms}|{ms}[s{j}]")
        mix.append(f"[s{j}]")
    fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[aout]" if len(mix) > 1
              else "[voice]anull[aout]")
    graph = os.path.join(workdir, "graph.txt")
    with open(graph, "w") as f:
        f.write(";\n".join(fc))
    preset, crf = ("ultrafast", "28") if preview else ("medium", "20")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex_script", graph,
           "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", preset,
           "-crf", crf, "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)


# ───────────── черновик CapCut ─────────────

def _norm_y(px: float) -> float:
    """Координата CapCut: 0 — центр кадра, −1 — верх, +1 — низ."""
    return round((px - H / 2) / (H / 2), 4)


def capcut_spec(edl, src, info, tl, subs, spans, pics, sounds):
    speed = tl.speed
    main = []
    for i, ((s, e), o) in enumerate(zip(tl.segments, tl.starts)):
        main.append({"ref": f"clip{i}", "path": src, "sourceStart": s, "duration": round((e - s) / speed, 3),
                     "start": o, "speed": speed})
    ops = []
    # наезды: ключевые кадры масштаба на тех кусках, через которые проходит наезд
    for sp in spans:
        for i, ((s, e), o) in enumerate(zip(tl.segments, tl.starts)):
            d = (e - s) / speed
            a, b = max(sp["t0"], o), min(sp["t1"], o + d)
            if b - a < 0.05:
                continue

            def z_at(t):
                return sp["z0"] + (sp["z1"] - sp["z0"]) * (t - sp["t0"]) / max(0.001, sp["t1"] - sp["t0"])
            ref = f"clip{i}"
            ra, rb = round(a - o, 3), round(b - o, 3)
            if ra > 0.04:
                ops.append({"op": "keyframe", "target": ref, "property": "scale", "time": round(ra - 0.04, 3), "value": 1.0})
            ops.append({"op": "keyframe", "target": ref, "property": "scale", "time": ra, "value": round(z_at(a), 3)})
            ops.append({"op": "keyframe", "target": ref, "property": "scale", "time": rb, "value": round(z_at(b), 3)})
            if rb + 0.04 < d:
                ops.append({"op": "keyframe", "target": ref, "property": "scale", "time": round(rb + 0.04, 3), "value": 1.0})
    tracks = [{"type": "video", "name": "Видео", "items": main}]
    if pics:
        items = []
        for k, p in enumerate(pics):
            img = Image.open(p["png"])
            fit = min(W / img.width, H / img.height)       # CapCut сам вписывает картинку в кадр
            items.append({"ref": f"pic{k}", "path": p["png"], "type": "photo", "start": p["t0"],
                          "duration": round(p["t1"] - p["t0"], 3), "scale": round(p["w"] / (img.width * fit), 4),
                          "y": _norm_y(PIC_Y.get(p["pos"], 1420))})
        tracks.append({"type": "video", "name": "Картинки", "items": items})
    st = edl.get("subs") or {}
    if subs and st.get("on", True):
        tracks.append({"type": "text", "name": "Субтитры", "items": [
            {"ref": f"sub{k}", "text": s["text"], "start": s["t0"], "duration": round(s["t1"] - s["t0"], 3),
             "fontSize": float(st.get("capcut_size", 13)), "color": st.get("color", "#FB7D7E"),
             "y": _norm_y(SUB_Y.get(st.get("pos", "top"), 210))} for k, s in enumerate(subs)]})
    audio = [{"path": s["file"], "start": s["t0"], "volume": s["vol"],
              "duration": round(min(4.0, probe(s["file"])["duration"], tl.total - s["t0"]), 3)}
             for s in sounds if tl.total - s["t0"] > 0.1]
    music = edl.get("music") or {}
    if music.get("file") and os.path.exists(music["file"]):
        md = probe(music["file"])["duration"]
        t = 0.0
        while t < tl.total - 0.1:    # музыку короче ролика повторяем
            audio.append({"path": music["file"], "start": round(t, 3), "duration": round(min(md, tl.total - t), 3),
                          "volume": float(music.get("vol") or 0.15)})
            t += md
    if audio:
        tracks.append({"type": "audio", "name": "Звуки и музыка", "items": audio})
    spec = {"name": edl.get("name") or "Ролик", "width": W, "height": H, "fps": FPS, "tracks": tracks}
    if ops:
        spec["operations"] = ops
    return spec


def main():
    job = os.path.abspath(sys.argv[1])
    preview = "--preview" in sys.argv
    edl = json.load(open(os.path.join(job, "edl.json"), encoding="utf-8"))
    os.chdir(job)   # относительные пути в листе считаются от папки ролика
    for key in ("pictures", "sounds"):
        for x in edl.get(key) or []:
            x["file"] = os.path.abspath(os.path.expanduser(x["file"]))
    if (edl.get("music") or {}).get("file"):
        edl["music"]["file"] = os.path.abspath(os.path.expanduser(edl["music"]["file"]))
    words = json.load(open(os.path.join(job, "words.json"), encoding="utf-8"))
    src = os.path.abspath(os.path.expanduser(edl["source"]))
    info = probe(src)
    keep = edl.get("keep") or [[words[0]["i"], words[-1]["i"]]]
    speed = max(0.5, min(2.0, float(edl.get("speed") or 1.0)))
    tl = Timeline(segments_from(words, keep, info["duration"]), speed)
    kw = kept_words(words, tl, edl.get("fixes") or {})
    by_i = {w["i"]: w for w in kw}
    st = edl.get("subs") or {}
    subs = sub_chunks(kw, int(st.get("words", 2)), st.get("case", "lower")) if st.get("on", True) else []
    for x in subs:
        x["t1"] = min(x["t1"], tl.total)
    subs = [x for x in subs if x["t1"] - x["t0"] > 0.1]
    spans = zoom_spans(edl, by_i, tl.total)
    workdir = os.path.join(job, "work")
    os.makedirs(workdir, exist_ok=True)
    pics = picture_items(edl, by_i, tl.total, workdir)
    sounds = sound_items(edl, by_i)
    bws = bw_spans(edl, by_i)
    name = re.sub(r"[^\w\- ]+", "", edl.get("name") or "ролик").strip() or "ролик"
    out = os.path.join(job, ("превью " if preview else "") + name + ".mp4")
    render_mp4(src, info, tl, subs, spans, pics, sounds, bws, edl, workdir, out, preview)
    report = {"mp4": out, "source_seconds": round(info["duration"], 1), "out_seconds": tl.total,
              "pieces": len(tl.segments), "zooms": len(spans), "pictures": len(pics), "sounds": len(sounds),
              "subtitles": len(subs), "zoom_times": [s["t0"] for s in spans], "picture_times": [p["t0"] for p in pics]}
    spec = capcut_spec(edl, src, info, tl, subs, spans, pics, sounds)
    # пересборка не трогает прежний черновик (вдруг его уже правили руками): новый получает номер
    store = os.environ.get("CAPCUT_DRAFT_DIR") or os.path.expanduser(
        "~/Movies/CapCut/User Data/Projects/com.lveditor.draft")
    base, n = spec["name"], 1
    while os.path.exists(os.path.join(store, spec["name"])):
        n += 1
        spec["name"] = f"{base} {n}"
    report["capcut_draft"] = spec["name"]
    with open(os.path.join(job, "capcut.json"), "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    report["capcut_spec"] = os.path.join(job, "capcut.json")
    if bws:
        report["capcut_note"] = "чёрно-белые вставки есть только в mp4, в CapCut их нужно добавить фильтром вручную"
    if "--no-capcut" not in sys.argv and not preview:
        cli = shutil.which("capcut")
        if not cli:
            report["capcut"] = "capcut-cli не установлен: npm install -g capcut-cli"
        else:
            r = subprocess.run([cli, "compile", report["capcut_spec"]], capture_output=True, text=True)
            report["capcut"] = (r.stdout or r.stderr).strip()[-1500:]
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
