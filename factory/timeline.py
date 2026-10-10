"""Монтаж по готовому плану (как в CapCut): куски исходника, ускорение, зумы, картинки, эффекты, субтитры.

Все времена в плане — время готового ролика (уже после ускорения).

segments: [{"src": начало в исходнике, "out": начало в ролике, "len": длительность в ролике}]
          из исходника берётся len * speed секунд.
words:    [{"word", "start", "end", "seg"}] — слова во времени ролика.
zooms:    [{"t0", "t1", "z0", "z1"}] — крупность по центру кадра; z0 == z1 — резкий «наезд».
cards:    [{"image", "t0", "t1", "box": [x, y, ширина], "anim": None | "slide_right" | "slide_left" | "rock"}]
effects:  [{"type": "glow", "t0", "t1"}] — зелёное свечение.

t0/t1 — смонтировать только этот отрезок ролика (на слабом компьютере ролик собирают частями).
"""
import os
import subprocess
import tempfile

from PIL import Image

from .edit import FONTS, FPS, H, W, build_ass, make_card
from .styles import STYLES

SLIDE = 0.3  # сколько секунд картинка выезжает

# огонь (как эффект «Пламя» в CapCut): рябь из синусов, языки ползут вверх;
# первые 0.8 с горит весь кадр, потом огонь уходит к краям и низу
_N = "(0.5+0.25*sin(X/28+T*3+1.5*sin(Y/40+T*6))+0.25*sin(Y/22+T*10+1.5*sin(X/30-T*4)))"
_E = "max(abs(X-W/2)/(W/2),Y/H)"
_Q = "clip((T-0.8)/0.4,0,1)*1.3"
_I = f"clip({_N}*1.6-0.1-(1-{_E})*{_Q},0,1)"
_FIRE = (f"r='255*pow({_I},0.4)':g='235*pow({_I},1.0)':b='120*pow({_I},2.5)':"
         f"a='255*clip({_I}*1.3,0,1)*clip((0.95-T)*9+0.55,0.55,1)'")


def _zoom_expr(zooms):
    expr = "1"
    for z in sorted(zooms, key=lambda z: -z["t0"]):
        a, b = z["t0"], z["t1"]
        val = f"{z['z0']}" if z["z0"] == z["z1"] else f"{z['z0']}+({z['z1']}-{z['z0']})*(on/{FPS}-{a:.3f})/{b - a:.3f}"
        expr = f"if(between(on/{FPS},{a:.3f},{b:.3f}),{val},{expr})"
    return expr


def _shift(items, t0, t1):
    out = []
    for x in items:
        if x["t1"] <= t0 or x["t0"] >= t1:
            continue
        out.append(dict(x, t0=x["t0"] - t0, t1=x["t1"] - t0))
    return out


def render_timeline(src, segments, words, out_path, style="polina", zooms=(), cards=(), effects=(), speed=None,
                    t0=0.0, t1=None, preset="veryfast", workdir=None, audio_filter="highpass=f=80", sounds=(),
                    grade=True, subs=True):
    """sounds: [{"file", "t", "vol", "dur"}] — мемные звуки поверх голоса (dur — обрезать звук).
    grade=False, subs=False — ролик уже смонтирован (у Полины свои цвет и субтитры), только добавляем шутки.
    effects также принимает {"type": "clip", "file", "t0", "t1"} — видео-эффект на чёрном (или "key": "green")."""
    st = STYLES[style]
    speed = speed or st.get("speed", 1.0)
    end = segments[-1]["out"] + segments[-1]["len"]
    t1 = min(t1 or end, end)
    total = t1 - t0
    work = workdir or tempfile.mkdtemp(prefix="timeline_")
    os.makedirs(work, exist_ok=True)

    # куски исходника, попавшие в окно
    pieces = []
    for s in segments:
        a, b = max(s["out"], t0), min(s["out"] + s["len"], t1)
        if b - a > 0.01:
            pieces.append((s["src"] + (a - s["out"]) * speed, (b - a) * speed))
    inputs, fc = [], []
    for k, (ss, dur) in enumerate(pieces):
        inputs += ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", src]
        fc.append(f"[{k}:v]setpts=PTS-STARTPTS[v{k}]")
        fc.append(f"[{k}:a]asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st={max(0, dur - 0.02):.3f}:d=0.02[a{k}]")
    n = len(pieces)
    fc.append("".join(f"[v{k}][a{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")

    zl = _shift(zooms, t0, t1)
    fc.append(
        f"[vc]setpts=(PTS-STARTPTS)/{speed},fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,"
        f"zoompan=z='{_zoom_expr(zl)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
        f"{st['grade'] if grade else 'null'}[v_base]"
    )
    last = "v_base"
    for k, e in enumerate(_shift(effects, t0, t1)):
        if e["type"] == "glow":
            fc.append(f"[{last}]split[g{k}a][g{k}b]")
            fc.append(f"[g{k}b]colorchannelmixer=rr=0.45:gg=1.25:bb=0.45,gblur=sigma=28,eq=brightness=0.06[g{k}c]")
            fc.append(f"[g{k}a][g{k}c]blend=all_mode=screen:all_opacity=0.65:"
                      f"enable='between(t,{e['t0']:.3f},{e['t1']:.3f})'[g{k}]")
            last = f"g{k}"

        if e["type"] == "bw":  # чёрно-белая вставка на ироничной реплике, как у Полины
            fc.append(f"[{last}]hue=s=0:enable='between(t,{e['t0']:.3f},{e['t1']:.3f})'[bw{k}]")
            last = f"bw{k}"

    extra = 0  # входы-эффекты (огонь) идут после кусков исходника
    for k, e in enumerate(_shift(effects, t0, t1)):
        if e["type"] != "fire":
            continue
        a, b = max(0.0, e["t0"]), min(total, e["t1"])
        inputs += ["-f", "lavfi", "-t", f"{e['t1'] - e['t0']:.3f}", "-i", "color=black:s=270x480:r=30"]
        start = e["t0"]
        fc.append(f"[{n + extra}:v]format=rgba,geq={_FIRE},scale={W}:{H},gblur=sigma=10,"
                  f"setpts=PTS-STARTPTS+{start:.3f}/TB,trim=start={a:.3f},setpts=PTS[f{k}]")
        fc.append(f"[{last}][f{k}]overlay=eof_action=pass:enable='between(t,{a:.3f},{b:.3f})'[fo{k}]")
        last = f"fo{k}"
        extra += 1

    for k, e in enumerate(_shift(effects, t0, t1)):
        if e["type"] != "clip":
            continue
        a, b = max(0.0, e["t0"]), min(total, e["t1"])
        inputs += ["-stream_loop", "-1", "-ss", f"{max(0.0, -e['t0']):.3f}", "-t", f"{b - a:.3f}", "-i", e["file"]]
        key = ("chromakey=0x00B140:0.18:0.08" if e.get("key") == "green"
               else "lumakey=threshold=0:tolerance=0.12:softness=0.3")
        fc.append(f"[{n + extra}:v]scale={W}:{H},fps={FPS},format=yuva420p,{key},"
                  f"setpts=PTS-STARTPTS+{a:.3f}/TB[x{k}]")
        fc.append(f"[{last}][x{k}]overlay=eof_action=pass:enable='between(t,{a:.3f},{b:.3f})'[xo{k}]")
        last = f"xo{k}"
        extra += 1

    for k, c in enumerate(_shift(cards, t0, t1)):
        idx = n + extra + k
        png = os.path.join(work, f"card{k}.png")
        x, y, w = c["box"]
        make_card(c["image"], png, None, width=int(w))  # без рамки, ширина w
        cw, ch = Image.open(png).size
        a, b = max(0.0, c["t0"]), min(total, c["t1"])
        inputs += ["-loop", "1", "-t", f"{b - a + 0.1:.2f}", "-i", png]
        rock = ""
        if c.get("anim") == "rock":
            rock = ",rotate=a='0.07*sin(2*PI*1.2*t)':ow='hypot(iw,ih)':oh=ow:c=none"
            d = (cw ** 2 + ch ** 2) ** 0.5  # rotate делает холст шире — держим центр картинки на месте
            x, y = round(x - (d - cw) / 2), round(y - (d - ch) / 2)
        pop = ",fade=t=in:st=0:d=0.12:alpha=1" if c.get("anim") in ("pop", "drop") and c["t0"] >= 0 else ""
        fc.append(f"[{idx}:v]format=rgba{rock}{pop},setpts=PTS-STARTPTS+{a:.3f}/TB[c{k}]")
        start = c["t0"]  # может быть < 0, если выезд начался в прошлой части
        if c.get("anim") == "slide_right":
            xe = f"if(lt(t,{start + SLIDE:.3f}),{x}+(W-{x})*(1-(t-{start:.3f})/{SLIDE}),{x})"
        elif c.get("anim") == "slide_left":
            xe = f"if(lt(t,{start + SLIDE:.3f}),{x}-(w+{x})*(1-(t-{start:.3f})/{SLIDE}),{x})"
        else:
            xe = f"{x}"
        ye = f"if(lt(t,{start + SLIDE:.3f}),{y}-({y}+h)*(1-(t-{start:.3f})/{SLIDE}),{y})" if c.get("anim") == "drop" else f"{y}"
        fc.append(f"[{last}][c{k}]overlay=x='{xe}':y='{ye}':eof_action=pass:enable='between(t,{a:.3f},{b:.3f})'[o{k}]")
        last = f"o{k}"

    if not subs:
        words = []
    local = [dict(w, start=w["start"] - t0, end=w["end"] - t0) for w in words if t0 <= w["start"] < t1]
    ass_path = os.path.join(work, "subs.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(build_ass(local, style, total, None, None, 0.0))
    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts_esc = FONTS.replace(":", "\\:")
    if subs:
        fc.append(f"[{last}]ass='{ass_esc}':fontsdir='{fonts_esc}',format=yuv420p[vout]")
    else:
        fc.append(f"[{last}]format=yuv420p[vout]")
    sfx = [x for x in sounds if t0 - 4 < x["t"] < t1]
    if not sfx:
        fc.append(f"[ac]atempo={speed},{audio_filter},aresample=48000[aout]")
    else:
        fc.append(f"[ac]atempo={speed},{audio_filter},aresample=48000,aformat=channel_layouts=stereo[voice]")
        first = sum(1 for i in range(len(inputs)) if inputs[i] == "-i")
        labels = []
        for k, x in enumerate(sfx):
            inputs += ["-i", x["file"]]
            t = x["t"] - t0
            trim = f"atrim=start={-t:.3f},asetpts=PTS-STARTPTS," if t < 0 else ""
            delay = int(max(0.0, t) * 1000)
            if x.get("dur"):
                d = x["dur"] - max(0.0, -t)
                trim += f"atrim=end={max(0.05, d):.3f},afade=t=out:st={max(0.0, d - 0.25):.3f}:d=0.25,"
            fc.append(f"[{first + k}:a]{trim}aresample=48000,aformat=channel_layouts=stereo,"
                      f"volume={x.get('vol', 0.7)},adelay={delay}[s{k}]")
            labels.append(f"[s{k}]")
        fc.append(f"[voice]{''.join(labels)}amix=inputs={len(sfx) + 1}:duration=first:normalize=0[aout]")

    graph = os.path.join(work, "graph.txt")
    with open(graph, "w") as f:
        f.write(";\n".join(fc))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex_script", graph,
           "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", preset,
           "-crf", "20", "-r", str(FPS), "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", out_path]
    subprocess.run(cmd, check=True)
    return total
