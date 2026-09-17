# Gradient Banding on Android Video — Diagnosis & Encoding Playbook

**Purpose.** This is a complete, self-contained runbook for eliminating **color banding** (visible "steps" in smooth gradients) in short hero videos delivered to an **Android app that renders through a Jetpack Compose `TextureView` (Media3/ExoPlayer)** — without introducing visible grain and while keeping the file small.

It is written to be **followed by an automated agent** _and_ read by **developers** and **After Effects animators**. If a new video asset bands, start at [§1 TL;DR](#1-tldr-the-real-solution) and [§9 Decision tree](#9-decision-tree-for-a-future-agent).

> Context this was derived from: a Gemini "Fold" hero video (2172×2006, 25 fps, ~11.3 s) with a very shallow dark gradient that banded on-device and in VLC. Every theory below was actually tested with ffmpeg on macOS. The winning approach is in §1 and §6.

---

## Table of contents

1. [TL;DR — the real solution](#1-tldr-the-real-solution)
2. [Symptoms & how to reproduce/measure](#2-symptoms--how-to-measure)
3. [How the Android video pipeline actually works](#3-how-the-android-video-pipeline-actually-works)
4. [Root cause (the physics)](#4-root-cause-the-physics)
5. [Every theory we went through (incl. dead-ends)](#5-every-theory-we-went-through)
6. [The real solution — generalized ffmpeg recipe for ANY video](#6-the-real-solution-generalized-ffmpeg-recipe)
7. [Diagnostic & verification toolbox (copy-paste)](#7-diagnostic--verification-toolbox)
8. [Tuning guide](#8-tuning-guide)
9. [Decision tree for a future agent](#9-decision-tree-for-a-future-agent)
10. [After Effects guide for animators](#10-after-effects-guide-for-animators)
11. [Developer / Android integration notes](#11-developer--android-integration-notes)
12. [Empirical data from the original investigation](#12-empirical-data-from-the-original-investigation)
13. [Gotchas & environment limitations](#13-gotchas--environment-limitations)
14. [Glossary](#14-glossary)

---

## 1. TL;DR — the real solution

Banding on a shallow gradient displayed at 8-bit **cannot be fixed by bit depth** (the `TextureView` samples video to 8-bit RGB regardless of a 10-bit source). It is fixed by **dither** that **survives the video codec**. Three things all matter:

1. **Encode full-range** (`color_range=pc`, `scale=out_range=full`). Limited range (16–235) throws away ~16% of the code values a shallow gradient desperately needs.
2. **Bake a _static_ dither pattern** into the video (a fixed noise tile, not moving grain). A static pattern is inter-predicted almost for free by VP9, so it survives compression. Moving/temporal grain gets quantized away and the bands come back.
3. **Disable the encoder's smoothing** (`-auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0`) so it doesn't average the dither back out.

To avoid _visible_ grain (QA's other complaint): make the dither **fine (not blurred/coarse)** and **confine it to the shadows** (the only place bands appear) with a luma mask.

**Winning encode** (VP9 Profile 0, 8-bit, full-range, no audio):

```bash
IN=input.mov
OUT=output.webm
W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width  -of csv=p=0 "$IN")
H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$IN")
FR=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$IN" | awk -F/ '{printf "%d", ($2? $1/$2 : $1)}')

# 1) one static fine-noise tile at the video's exact resolution
ffmpeg -y -f lavfi -i "color=c=gray:s=${W}x${H}" -vf "noise=alls=40:allf=u" -frames:v 1 noise.png

# 2) encode: full-range VP9 8-bit + fine, shadow-masked, static dither
ffmpeg -y -i "$IN" -framerate "$FR" -loop 1 -i noise.png \
 -filter_complex "\
[0:v]scale=out_range=full,format=gbrp,split=3[b1][b2][b3];\
[1:v]format=gbrp[n];\
[b1][n]blend=all_mode=grainmerge:all_opacity=0.10:shortest=1[grained];\
[b2]format=gray,lut=c0='clip((170-val)*3.64\,0\,255)',format=gbrp[mask];\
[b3][grained][mask]maskedmerge,format=yuv420p[o]" \
 -map "[o]" -shortest -c:v libvpx-vp9 -crf 12 -b:v 0 \
 -color_range pc -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
 -auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0 -row-mt 1 -tile-columns 2 -threads 8 -an \
 "$OUT"
```

Then **verify** with the tools in [§7](#7-diagnostic--verification-toolbox): the dark-gradient "longest flat run" should be small (≤ ~20 px is imperceptible; the lossless master is ~5 px), and a native 1:1 crop should show no visible grain.

> ⚠️ The single most common mistake (we hit it): if you `-loop 1` an image input you **must** end the stream, or ffmpeg encodes forever. Keep `shortest=1` on the `blend` **and** `-shortest` on the output (or add `-frames:v <N>`). See [§13](#13-gotchas--environment-limitations).

---

## 2. Symptoms & how to measure

**Symptoms**

- Visible concentric/horizontal "steps" in smooth gradients, worst in **dark** areas.
- Often **invisible in a downscaled thumbnail** but **visible full-screen** and in **VLC** (VLC does not add display dithering, so it is the honest ground-truth viewer — trust VLC over a scaled screenshot).

**Do not trust a boosted+downscaled screenshot alone.** Downscaling averages neighboring pixels and hides bands. Always inspect a **native-resolution 1:1 crop** and/or measure numerically.

**The one metric that actually correlates with banding: "longest flat run".**
Take a 1-px-wide vertical line through the gradient, and measure the longest run of _identical_ luma values. Grain/dither constantly perturbs the value, so a dithered gradient has tiny runs (~5 px). A banded gradient has one value held flat for tens–hundreds of px.

| longest flat run | verdict                             |
| ---------------- | ----------------------------------- |
| ≤ ~15 px         | dithered, band-free (master ≈ 5 px) |
| ~20–60 px        | faint banding, borderline           |
| > ~100 px        | obvious bands                       |

See [§7](#7-diagnostic--verification-toolbox) for the exact commands.

---

## 3. How the Android video pipeline actually works

Facts established for this app (Compose + Media3 ExoPlayer, `ContentFrame` with `SURFACE_TYPE_TEXTURE_VIEW`, `ContentScale.Crop`, nav uses Compose `fadeIn()/fadeOut()`):

- **`TextureView` samples video into an 8-bit RGB texture.** Even a 10-bit source is truncated to 8-bit on the way to the screen. → _You cannot buy your way out of banding with 10-bit if you render through `TextureView`._
- **`SurfaceView` can preserve 10-bit better**, but it composites in its own layer and **does not honor Compose alpha/`fade` animations**, which breaks the app's screen transitions. So `SurfaceView` was rejected.
- **VP9 Profile support on Android:**
  - **Profile 0 = 8-bit 4:2:0** — universal (mandatory from API 24), lightest decode. **Use this.**
  - Profile 2 = 10-bit 4:2:0 — decodable API 24+ but heavier, and pointless here (TextureView truncates).
  - Profiles 1/3 (4:2:2 / 4:4:4) — **not** decodable on Android.
- **Emulator:** cannot decode **HEVC**. VP9 decodes in software. H.264 High 10 (10-bit) does **not** decode on Android.
- **Color range** (full/`pc` vs limited/`tv`) is signaled in the VP9 bitstream and honored by Media3. Full range preserves more code values for shallow gradients.

---

## 4. Root cause (the physics)

Banding here is the product of a chain, not a single codec bug:

1. **The gradient is extremely shallow.** In the original asset the dark ramp spanned only ~14 code values over ~1800 px (measured). At 8-bit that is only a handful of distinct levels — right at the edge of visible stepping _before any compression_.
2. **8-bit display quantization.** `TextureView` = 8-bit. A shallow ramp quantized to 8-bit has few steps.
3. **Limited range makes it worse.** Encoding `tv` range squeezes 0–255 into 16–235, discarding ~16% of the already-scarce levels.
4. **The codec removes the dither that was hiding the steps.** The master had fine film-grain/dither that broke up the bands. Lossy codecs (HEVC, and VP9 at normal CRF) zero the tiny high-frequency AC coefficients that _are_ that grain, collapsing the ramp into flat plateaus → bands reappear. This is why the same gradient looked fine in the lossless master but banded after encoding.

**Corollary:** the cure is to **guarantee dither at the moment of display** and make sure it **survives the codec**. Because a static pattern costs almost nothing to carry (temporal prediction), a _static_ dither is the efficient way to do it. Fine + shadow-limited keeps it invisible.

---

## 5. Every theory we went through

Chronological, including the dead-ends — so a future agent doesn't repeat them.

| #   | Theory / attempt                                                | What we did                                                              | Result                                                                                                                                             | Verdict                          |
| --- | --------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| 1   | "Emulator won't play the file → wrong codec"                    | Tried HEVC                                                               | HEVC undecodable on emulator                                                                                                                       | ✅ true but unrelated to banding |
| 2   | "Switch codec fixes look" (H.264 vs HEVC vs VP9)                | Compared encodes                                                         | Look differences were really about grain retention, not codec identity                                                                             | ➖ partial                       |
| 3   | "Go 10-bit (VP9 Profile 2 / H.264 High10)"                      | 10-bit encodes                                                           | Looked band-free **only because they inherited the master's grain**; `TextureView` truncates to 8-bit anyway; H.264 10-bit won't decode on Android | ❌ dead-end                      |
| 4   | "It's a bit-depth problem"                                      | Measured actual code values                                              | The 16-bpc pipeline was intact; steps were 1 code value, not 4 → not an 8-bit quantization _bottleneck_, it's a _shallow-gradient_ problem         | ❌ reframed                      |
| 5   | "Rec.601 vs Rec.709 matrix"                                     | Checked tags                                                             | Color matrix is unrelated to banding; keep Rec.709 for HD                                                                                          | ❌ red herring                   |
| 6   | "Export settings in the player can fix baked banding"           | —                                                                        | Banding baked into a file can't be undone downstream                                                                                               | ❌                               |
| 7   | "Lossy codecs strip film grain that was dithering the gradient" | Boosted-shadow crops; PNG-size heuristic (grainy crop compresses larger) | Confirmed: HEVC/low-bitrate VP9 strip the fine grain → bands                                                                                       | ✅ key insight                   |
| 8   | "Just lower CRF to keep grain"                                  | VP9 CRF sweep                                                            | Works but expensive: needed ~CRF 8 (≈ big files) because **temporal** grain is costly; and that surviving grain is the "noticeable" kind           | ➖ works, not small              |
| 9   | "Full range vs limited range matters"                           | Encoded both                                                             | Full range measurably better (more distinct levels)                                                                                                | ✅ real factor                   |
| 10  | "Static dither instead of temporal grain"                       | Overlaid a fixed noise tile, disabled encoder smoothing                  | **Breakthrough**: survives VP9 cheaply → band-free at small size                                                                                   | ✅✅ core of solution            |
| 11  | "Coarser/stronger static dither to survive higher CRF"          | blur σ≈1.0, higher amplitude                                             | Band-free and tiny (~17 MB) **but QA said too grainy** — coarse low-freq noise is very visible even at low amplitude                               | ❌ overshoot                     |
| 12  | "Fine + shadow-masked static dither"                            | fine (no blur) noise, luma mask to shadows only, moderate CRF            | **Real solution**: grain cleaner than the master, no visible bands                                                                                 | ✅✅✅ final                     |

Also proven along the way:

- **PNG-size heuristic** (compress a boosted crop; larger PNG = more grain retained) is a quick proxy but **under-detects banding when the crop is downscaled** — use the flat-run metric and native crops instead.
- **VP9 has no film-grain synthesis** (AV1 does). That's why VP9 forces you to _bake_ dither. If you can guarantee an AV1 decode path with film-grain-synthesis support, that's an alternative — but Android hardware AV1 + FGS support is inconsistent, so we did not rely on it.

---

## 6. The real solution — generalized ffmpeg recipe

### 6.1 What each part does

| Flag / filter                                         | Why                                                                                |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `scale=out_range=full` + `-color_range pc`            | Full-range: keep every code value the shallow gradient has                         |
| `noise=alls=40:allf=u` on a single frame → tile       | A **static** fine dither pattern (uniform noise, ~±40 range pre-scale)             |
| `blend=all_mode=grainmerge:all_opacity=0.10`          | Adds `(noise-128) * opacity` → ~±2–3 code dither. `grainmerge` = `A + B − 128`     |
| `lut=c0='clip((170-val)*3.64,0,255)'` → `maskedmerge` | Apply dither **only in shadows** (below luma ~170). Bright/flat UI stays clean     |
| `-auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0`        | Turn off VP9 temporal filtering / adaptive quant that would smooth the dither away |
| `-crf 12 -b:v 0`                                      | Quality-targeted VP9; lower CRF = more dither survives (see tuning)                |
| `-an`                                                 | Drop audio (hero loops are silent; saves size)                                     |
| `shortest=1` + `-shortest`                            | Terminate the looped-image stream (**required**, see §13)                          |

### 6.2 The command (portable, any resolution/fps)

```bash
IN=input.mov          # your lossless / high-quality master
OUT=output.webm
W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width  -of csv=p=0 "$IN")
H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$IN")
FR=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$IN" | awk -F/ '{printf "%d", ($2? $1/$2 : $1)}')

# 1) static fine-noise tile at the exact resolution (must match the video)
ffmpeg -y -f lavfi -i "color=c=gray:s=${W}x${H}" -vf "noise=alls=40:allf=u" -frames:v 1 noise.png

# 2) encode
ffmpeg -y -i "$IN" -framerate "$FR" -loop 1 -i noise.png \
 -filter_complex "\
[0:v]scale=out_range=full,format=gbrp,split=3[b1][b2][b3];\
[1:v]format=gbrp[n];\
[b1][n]blend=all_mode=grainmerge:all_opacity=0.10:shortest=1[grained];\
[b2]format=gray,lut=c0='clip((170-val)*3.64\,0\,255)',format=gbrp[mask];\
[b3][grained][mask]maskedmerge,format=yuv420p[o]" \
 -map "[o]" -shortest -c:v libvpx-vp9 -crf 12 -b:v 0 \
 -color_range pc -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
 -auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0 -row-mt 1 -tile-columns 2 -threads 8 -an \
 "$OUT"
```

### 6.3 Simpler variant (no shadow mask)

If you don't know where the banding sits, or the whole frame is dark, you can dither everywhere at low amplitude. Slightly grainier but robust:

```bash
ffmpeg -y -i "$IN" -framerate "$FR" -loop 1 -i noise.png \
 -filter_complex "[0:v]scale=out_range=full,format=gbrp[v];[1:v]format=gbrp[n];\
[v][n]blend=all_mode=grainmerge:all_opacity=0.10:shortest=1,format=yuv420p[o]" \
 -map "[o]" -shortest -c:v libvpx-vp9 -crf 12 -b:v 0 \
 -color_range pc -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
 -auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0 -row-mt 1 -tile-columns 2 -threads 8 -an "$OUT"
```

### 6.4 Fallback: just preserve the master's own grain (no added dither)

If the master already has good fine grain and you don't mind a larger file, low CRF preserves it:

```bash
ffmpeg -y -i "$IN" -vf "scale=out_range=full,format=yuv420p" -c:v libvpx-vp9 -crf 8 -b:v 0 \
 -color_range pc -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
 -auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0 -row-mt 1 -tile-columns 2 -threads 8 -an "$OUT"
```

This yields the "intended" look but is the largest option (grain is expensive). Prefer §6.2.

---

## 7. Diagnostic & verification toolbox

> ffmpeg build note: some macOS builds **lack `zscale`, `drawtext`, and 16-bit `qtrle` decode**. Everything below avoids them. Python here uses only the standard library (no numpy/PIL).

### 7.1 Probe codec, pixel format, range, size

```bash
ffprobe -v error -select_streams v:0 \
 -show_entries stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,color_range,color_space \
 -show_entries format=duration,size,bit_rate,nb_streams \
 -of default=noprint_wrappers=1 "$FILE"
```

- `pix_fmt=yuv420p` = 8-bit; `yuv420p10le` = 10-bit; `rgb24` = 8-bit RGB; `rgb48`/`bgr48` = 16-bit RGB.
- `nb_streams=2` usually means an unwanted audio track (`-an` to drop).

### 7.2 Read the **true stored bit depth** of a QuickTime/MOV master (don't trust ffmpeg's decoded pix_fmt alone)

```bash
python3 - "$FILE" <<'PY'
import sys
data=open(sys.argv[1],'rb').read(4_000_000)
i=data.find(b'rle ')            # QuickTime Animation ('rle '); adjust fourcc for others
if i<0: print("no 'rle ' atom found"); raise SystemExit
entry=i-4
off=entry+4+4+6+2+2+2+12+2+2+4+4+4+2+32   # -> 'depth' field in VisualSampleEntry
depth=int.from_bytes(data[off:off+2],'big')
w=int.from_bytes(data[entry+28:entry+30],'big'); h=int.from_bytes(data[entry+30:entry+32],'big')
print(f"stored depth = {depth} bits/pixel  (24=8-bit RGB, 32=8-bit+alpha, 48=16-bit RGB)")
print(f"parsed WxH = {w}x{h}  (sanity check: should match the real resolution)")
PY
```

If `depth = 24` the "uncompressed" master is only **8-bit**, no matter what the animator selected. (16-bit "Trillions of Colors" ⇒ `depth = 48`, `pix_fmt = rgb48`, ~2× file size.)

### 7.3 The banding metric — "longest flat run" on the dark gradient

Pick an X column that passes through the gradient (e.g. `X=40`).

```bash
# extract frame N (e.g. 125) as raw 8-bit gray of the WHOLE frame
ffmpeg -y -v error -i "$FILE" -vf "select=eq(n\,125),format=gray" -frames:v 1 -f rawvideo frame.gray

python3 - frame.gray <<'PY'
import sys
W,H=2172,2006        # <-- set to your video's width,height
X=40                 # <-- a column through the gradient
d=open(sys.argv[1],'rb').read()
v=[d[y*W+X] for y in range(H)]
best=cur=1
for i in range(1,H):
    if v[i]==v[i-1]: cur+=1; best=max(best,cur)
    else: cur=1
print(f"distinct={len(set(v))} longest_flat_run={best}px  (<=~15 good, >~100 bad)")
PY
```

> Do NOT `crop` immediately after `select` in one filtergraph — a known ffmpeg reinit bug throws "non positive size for width '0'". Extract the whole frame with `format=gray`, then index the column in Python (as above).

### 7.4 Grain-visibility metric — stddev in flat patches

```bash
python3 - frame.gray <<'PY'
import sys
W,H=2172,2006
d=open(sys.argv[1],'rb').read()
def pstd(x0,y0,s=80):
    a=[d[(y0+j)*W+(x0+i)] for j in range(s) for i in range(s)]
    m=sum(a)/len(a); return m,(sum((x-m)**2 for x in a)/len(a))**0.5
for (x,y,l) in [(1000,150,'near-black'),(40,400,'dark'),(1400,1750,'glow')]:
    m,s=pstd(x,y); print(f"{l:11s} mean={m:5.1f} stddev={s:4.1f}")
PY
```

Lower stddev in flat patches = less visible grain. Compare candidate vs master; aim ≤ master.

### 7.5 Eyeball checks (always do both)

```bash
# native 1:1 crop of the gradient region -> what the eye/VLC sees
ffmpeg -y -v error -i "$FILE" -vf "select=eq(n\,125),crop=900:900:0:1150" -frames:v 1 native.png

# boosted stress-test (exaggerates any residual band)
ffmpeg -y -v error -i "$FILE" -vf "select=eq(n\,125),crop=900:900:0:1150,eq=gamma=2.4:brightness=0.2:contrast=1.5" -frames:v 1 boost.png
```

Open `native.png` (no grain visible?) and `boost.png` (no hard contour lines?). **Also open the actual `.webm` in VLC** for the final sign-off.

---

## 8. Tuning guide

Start from §6.2, then adjust one axis at a time and re-measure with §7.

| If…                                  | Change                                                                            | Effect                                 |
| ------------------------------------ | --------------------------------------------------------------------------------- | -------------------------------------- |
| Bands still visible (flat run > ~30) | Lower `-crf` (e.g. 12 → 10)                                                       | More dither survives; larger file      |
| Bands still visible                  | Raise `all_opacity` (0.10 → 0.14)                                                 | Stronger dither; risk of visible grain |
| Bands only in a specific tone        | Move mask threshold `170` toward that tone; adjust slope `3.64 = 255/(T−F)`       | Dither where needed only               |
| Too grainy (QA)                      | Keep noise **fine** (no `gblur`); lower `all_opacity`; tighten mask (lower `170`) | Less visible grain                     |
| Too grainy in brights                | Ensure mask is applied (§6.2, not §6.3)                                           | Brights stay clean                     |
| File too big                         | Raise `-crf` (12 → 14) and/or raise `all_opacity` a touch to compensate           | Smaller; watch banding                 |
| Grain looks "blobby"/dirty           | Reduce noise coarseness: remove any `gblur`, or `noise alls` smaller              | Finer, less perceptible                |

**Do NOT** coarsen the noise (`gblur`) to make it survive — coarse low-frequency noise is far more visible per unit amplitude (this is what QA rejected). Prefer **fine noise + lower CRF**.

Reference anchors (2172×2006, 11.3 s, from the original asset):

| approach                               | CRF   | opacity   | coarse? | mask? | size      | flat run  | grain               |
| -------------------------------------- | ----- | --------- | ------- | ----- | --------- | --------- | ------------------- |
| plain, keep temporal grain             | 8     | —         | —       | —     | 84 MB     | 11 px     | master-level        |
| coarse static (too grainy)             | 16    | 0.20      | σ1.0    | no    | 17 MB     | 16 px     | **too visible**     |
| **fine static, shadow-masked (final)** | 12    | 0.10      | no      | yes   | 42 MB     | 33 px     | cleaner than master |
| fine static, shadow-masked, smaller    | 13–14 | 0.10–0.12 | no      | yes   | ~25–30 MB | ~40–60 px | clean; check bands  |

---

## 9. Decision tree for a future agent

```
New asset bands on device / in VLC?
│
├─ 1. Probe it (§7.1) and read true bit depth (§7.2).
│     • If pix_fmt is 10-bit: irrelevant for TextureView (truncated). Proceed as 8-bit.
│     • If it has audio (nb_streams=2) and you don't need it: plan -an.
│
├─ 2. Measure the master's flat run (§7.3).
│     • Master flat run small (≤ ~15): master is fine; banding is introduced by ENCODING → go to 3.
│     • Master flat run large: banding is BAKED IN by the source → you cannot fully fix at encode.
│         Ask animators for a re-export (§10). As a stopgap, add stronger dither (§8) to mask it.
│
├─ 3. Encode with §6.2 (full-range + fine shadow-masked static dither, smoothing off, CRF 12).
│
├─ 4. Verify (§7.3–7.5): flat run ≤ ~20, native crop clean, boost has no hard lines, VLC OK.
│     • Bands remain → lower CRF / raise opacity / adjust mask (§8), re-verify.
│     • Too grainy → keep fine, lower opacity, tighten mask (§8), re-verify.
│
└─ 5. Confirm output tags: VP9 Profile 0, yuv420p, color_range=pc, bt709, no audio (§7.1).
      Hand to dev; sanity-check full-range rendering on a real device (§11).
```

---

## 10. After Effects guide for animators

There are **two ways** to work, pick based on who owns the final file:

- **Mode A — you deliver a master, devs encode.** Give devs a high-bit-depth, effectively-lossless master with no baked-in banding; they run §6. Covered in §10.1–§10.4.
- **Mode B — you own the whole thing and export the final Android-ready file yourself.** You bake the anti-banding dither into the comp and produce the deliverable. Covered in **§10.5–§10.7**. This reproduces the exact §1 result without a dev round-trip.

> Reality check: After Effects (and Adobe Media Encoder) **cannot natively emit VP9/WebM**, and the fnord WebM plugin can't set full-range or disable the encoder's temporal smoothing. So Mode B has two flavors — B1 (bake dither, then run one fixed ffmpeg line yourself) is **reliable**; B2 (bake dither, export straight to WebM via the fnord plugin) is **convenient but must be verified in VLC**. Details in §10.6.

### 10.1 Work in high bit depth

- **Project Settings → Color → Depth:** set to **16 bpc** (or **32 bpc float** for heavy glows/gradients). 8 bpc bakes banding immediately.
- Quick toggle: **Alt/Option-click** the bit-depth indicator at the bottom of the Project panel to cycle 8→16→32.

### 10.2 Kill banding _inside_ the comp (so it's not in the master)

Even at 16/32 bpc, long shallow gradients can step. Add a tiny dither over gradient areas:

- Add an adjustment layer over the gradient → **Effect → Noise & Grain → Add Grain** (or **Noise**), **very low** amount (e.g. Noise ~1–2%). Keep it subtle; the dev's encode will also protect the gradient.
- Alternatively animate the gradient in 32-bit and let AE's higher precision + a hint of grain avoid steps.
- Prefer **fine** grain over coarse; coarse grain is very visible after delivery.

> This is the light-touch version for **Mode A** masters. If you're owning the final export (**Mode B**), use the fuller, shadow-masked bake in §10.5 instead.

### 10.3 Export a proper master (pick ONE)

- **QuickTime Animation, "Trillions of Colors"** (16-bit, lossless). ⚠️ verify it's actually 16-bit — see pitfalls.
- **ProRes 4444** (12-bit, visually lossless, much smaller than Animation).
- **16-bit PNG or TIFF image sequence** (truly lossless, unambiguous bit depth).
- Rec.709 for HD deliverables. No audio needed for silent hero loops.

### 10.4 Pitfalls that silently downgrade to 8-bit (we hit this)

The "uncompressed" master we received was **8-bit** (`depth=24`) even though "Trillions of Colors" was reportedly selected. Check all of these:

1. **Output Module → Video Output → "Depth" dropdown** must literally read **"Trillions of Colors"** (this control, not the codec dialog, decides it). "Millions of Colors" = 8-bit.
2. **Project bit depth** must be **16/32 bpc**; otherwise "Trillions" just pads 8-bit data.
3. **Adobe Media Encoder** presets for QuickTime Animation often default to **8-bit** — its depth control is elsewhere and easy to miss. When in doubt, export from **AE's own Render Queue**.
4. Beware saved **Output Module templates** pinned to "Millions of Colors."

**How devs will verify your master** (share this so there's no back-and-forth): run §7.1 and §7.2. A real 16-bit Animation master reads `pix_fmt = rgb48` / `depth = 48` and is ~2× the size of an 8-bit one.

> Note: an 8-bit master **can** still be fine **if AE dithered on the 16→8 step** (project at 16 bpc, exporting 8-bit) — that's why our 8-bit master had good grain and no bands. But a true 16-bit master is the safer deliverable.

### 10.5 Mode B — bake the anti-banding dither into the comp (animator-owned)

This reproduces the §1 fix **inside After Effects**: a **fine, static, shadow-limited dither** baked into the pixels. Once it's baked, the gradient stays band-free through any reasonable encode — that's what lets you export the final file yourself.

The three properties that matter (same as §1): the dither must be **fine** (per-pixel, not blurry blobs), **static** (identical every frame — this is what survives compression), and **confined to the shadows** (so brights/UI stay clean and grain stays invisible).

**Step-by-step (AE 2023+):**

1. **Set the project to 16 bpc (or 32 bpc float).** (§10.1) Design the whole thing in high bit depth.
2. **Precompose your finished animation** into one layer — call it `CONTENT`.
3. **Add an Adjustment Layer** above `CONTENT`, name it `DITHER`.
4. On `DITHER`, apply **Effect → Noise & Grain → Noise**:
   - **Uncheck "Use Color Noise"** (luma-only dither is far less visible than RGB noise).
   - **Check "Clip Result Values".**
   - **Amount of Noise ≈ 2%** to start (≈ ±2–3 code values). This is _additive, static_ noise — the Noise effect uses the same pattern every frame, which is exactly what you want. Do **not** use Fractal Noise/Add Grain here (they flicker/animate).
5. **Confine the dither to shadows** (so it's invisible in brights):
   - **Duplicate `CONTENT`**, drag the copy **directly above `DITHER`**, name it `MATTE`.
   - Set `DITHER`'s **track matte to "Luma Inverted Matte"** (dither now appears only where the picture is dark).
   - On `MATTE`, add **Effect → Color Correction → Curves** (or Levels) and pull the curve so **highlights go to black and shadows to white** — this sets _where_ the dither fades out. Roughly: fully on below ~40% luma, off above ~66% luma (mirrors the ffmpeg `lut` threshold of ~170/255).
6. **Keep everything static:** no keyframes on the Noise effect or its Random Seed. The pattern must not move.
7. **Preview the real result:** temporarily switch the comp/preview to **8 bpc** (Alt/Option-click the bit-depth indicator) and zoom to 100%. The gradient should be smooth with no visible grain. Adjust the Noise **Amount** up if you still see faint bands, down if you see texture.

> Why static, not "nice moving grain": moving grain is expensive and the encoder throws it away, so the bands come back. A fixed pattern is nearly free for the codec to keep. See §4.

### 10.6 Mode B — export the final Android file yourself

Once the dither is baked (§10.5), pick one:

**B1 — Reliable (recommended): render a baked master, then one fixed ffmpeg line.**
You still own the deliverable; the ffmpeg step is mechanical and adds **no** dither (it's already baked), it only guarantees full-range + smoothing-off.

1. Render `CONTENT+DITHER` to a lossless/near-lossless master: **ProRes 4444** or a **PNG sequence** (Rec.709, no audio).
2. Run this once (no noise tile, no mask — the dither is already in the pixels):

```bash
ffmpeg -y -i baked_master.mov -vf "scale=out_range=full,format=yuv420p" \
 -c:v libvpx-vp9 -crf 12 -b:v 0 \
 -color_range pc -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
 -auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0 -row-mt 1 -tile-columns 2 -threads 8 -an \
 Gemini_Fold_final.webm
```

**If you rendered a PNG sequence instead of a `.mov`**, the only change is the input: point ffmpeg at the numbered files and give it the frame rate (AE has no frame rate baked into stills). For files like `frame_0000.png, frame_0001.png, …` at 25 fps, replace `-i baked_master.mov` with:

```bash
 -framerate 25 -start_number 0 -i "frame_%04d.png"
```

so the full command becomes:

```bash
ffmpeg -y -framerate 25 -start_number 0 -i "frame_%04d.png" \
 -vf "scale=out_range=full,format=yuv420p" \
 -c:v libvpx-vp9 -crf 12 -b:v 0 \
 -color_range pc -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
 -auto-alt-ref 0 -arnr-maxframes 0 -aq-mode 0 -row-mt 1 -tile-columns 2 -threads 8 -an \
 Gemini_Fold_final.webm
```

Adjust `%04d` to the actual zero-padding (e.g. `%05d` for 5 digits), `-start_number` to the first frame number, and `-framerate` to your comp's fps. `cd` into the folder of PNGs first, or use the full path in the pattern.

(Install ffmpeg once: `brew install ffmpeg` on macOS.)

**B2 — Convenient (no terminal): export straight to WebM with the fnord WebM plugin.**
Free plugin ("WebM" by fnordware) that adds WebM/VP9 output to AE's Render Queue.

1. Install the fnord WebM plugin; restart AE.
2. Render Queue → **Output Module → Format: WebM**. Codec: **VP9**. **8-bit.** **No audio.**
3. **Method: Constant Quality** (this is the true CRF-equivalent mode — the quality slider is ignored in VBR/Bitrate modes).
4. **Set quality via the slider using the map below.** In this plugin the slider does **not** set CRF directly — it sets the max quantizer, and the effective CRF is _half_ of that (`cq_level = max_q / 2`), so the slider only ever reaches CRF 0–31:

   | Slider    | 100 | 90  | 80  | 70  | 60  | 50  | 40  | 30  | 20  | 10  | 0   |
   | --------- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
   | **≈ CRF** | 0   | 3   | 6   | 10  | 13  | 16  | 19  | 22  | 25  | 28  | 31  |

   Rule of thumb: **`slider ≈ 100 − 3.2 × CRF`**. For our **CRF 12** target, set the slider to **≈ 62**.

5. **Use the advanced / custom-args field** to pin the exact recipe (these override the slider and call the real VP9 controls):

   ```
   --cq-level 12 --auto-alt-ref 0 --arnr-maxframes 0 --aq-mode 0
   ```

   `--cq-level 12` sets CRF directly (so you don't have to trust the slider); the other three **disable the temporal smoothing that eats dither** — the plugin _can_ do this via custom args.

6. **The one thing the plugin can't truly do: full range.** It always converts RGB→YUV as **studio range (16–235)** in code; `--color-range full` only flips the _signaling_ flag (which would mislabel the pixels, not recover them). So B2 loses ~16% of the shallow-gradient code values that B1 keeps. **Compensate by baking the dither a little stronger** (Noise **Amount ≈ 3–4%** in §10.5) and keeping CRF low (≤ 12).
7. **Always verify in VLC** (§10.7). If you see any bands, raise the baked Noise amount / lower `--cq-level`, or switch to B1.

> If in doubt, use **B1** — it's the only path that gives true full range. B2 is fine for delivery **if** you bake extra dither margin and verify.

### 10.7 Verify your own export (animator self-check)

Before you hand it off, confirm it yourself — no dev needed:

1. **Open the `.webm` in VLC**, full-screen, and look hard at the dark gradient. VLC does not hide bands (unlike some players/thumbnails), so it's the honest test. No steps = good.
2. **Scrub the whole clip** — bands sometimes only appear on certain frames as the gradient animates.
3. If you have a terminal, run the dev's numeric check (§7.3) — a **longest flat run ≤ ~20 px** means band-free. Also glance at a **native 1:1 crop** (§7.5) to confirm no visible grain.
4. Sanity-check size against §8; if it's many hundred MB, your quality/CRF is set far too high.

If it passes VLC + looks clean at 100%, it will look right in the app.

### 10.8 Tuning guide (After Effects controls)

The AE equivalent of §8. Change **one control at a time**, then re-preview at **8 bpc / 100%** (§10.5 step 7) and re-check the exported file in **VLC** (§10.7).

**Start here:** Noise **Amount ≈ 2%** (B1) or **3–4%** (B2) · matte fully on below ~40% luma, off above ~66% · Depth **16 bpc** · Working Space **Rec.709**, Linearize **Off** · export **CRF 12** (B1: `-crf 12` / B2: slider ≈ 62 or `--cq-level 12`) · Max Keyframe Distance **~125**.

| If…                                   | Change in AE                                                                                         | Effect                       |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------- |
| Bands still visible after export      | Raise Noise **Amount** (2% → 3–4%); and/or raise export quality (B2) / lower `-crf` (B1)             | More dither survives         |
| Bands only in the **darkest** tone    | On the `MATTE`, push **Curves/Levels** so shadows stay fully white higher up (widen the "on" region) | Dither reaches the band      |
| Bands in a **mid/brighter** area      | Extend the matte's white region toward that tone (raise the fade-out point)                          | Dither where it's needed     |
| Too grainy overall                    | Lower Noise **Amount**; tighten the matte (pull the "off" point darker)                              | Less visible grain           |
| Grain visible in **brights / UI**     | Confirm the **Luma Inverted track matte** is applied and Curves crush highlights to black            | Brights stay clean           |
| Grain looks **blobby / crawling**     | You're using **Add Grain/Fractal Noise** — switch to the **Noise** effect; uncheck Use Color Noise   | Finer, static dither         |
| Banding reappears **only in motion**  | Make the dither **static** — remove any keyframes on Noise / Random Seed                             | Pattern survives the codec   |
| Whole image looks **milky / washed**  | Set Working Space **Rec.709** + Linearize **Off** (or move to **32 bpc float**)                      | Correct, matched look        |
| Shadows look **crushed / posterized** | You're at **8 bpc** — switch project to **16 bpc** before baking                                     | Enough code values to dither |
| File too big                          | (B2) raise **Max Keyframe Distance** / lower quality a touch · (B1) raise `-crf` (12 → 14)           | Smaller; re-check bands      |

**Do NOT** coarsen the dither (Add Grain size, or blurring the Noise) to force it through the encoder — coarse low-frequency grain is far more visible per unit strength (same lesson as §8). Prefer **fine Noise + higher export quality**.

Mapping to the ffmpeg knobs (for cross-reference): Noise **Amount** ≈ `all_opacity`; **matte Curves threshold** ≈ the `lut` mask threshold (~170); **static Noise** ≈ the fixed noise tile; **export quality / CRF** ≈ `-crf`; **Max Keyframe Distance** ≈ `-g`.

---

## 11. Developer / Android integration notes

- **Keep `TextureView`** (Compose alpha/fade transitions depend on it). Do not switch to `SurfaceView` just for bit depth — it breaks the app's fade animations and TextureView is 8-bit anyway.
- **Ship VP9 Profile 0** (8-bit 4:2:0). Universal decode, lightest, and matches the 8-bit display path.
- **Full range:** the recipe tags `color_range=pc`. Media3/ExoPlayer honors it, but **spot-check on a real device** that blacks/gradients look right (full vs limited mismatch shows as washed-out or crushed shadows).
- **Per-form-factor assets:** phones vs tablets (`raw/` vs `raw-sw600dp/`) resolve via `R.raw.<name>`. Make sure the correct variant is replaced.
- **No audio** in hero loops (`-an`).
- File sizes: the final approach lands ~25–45 MB for an ~11 s 2172×2006 loop. That is normal and fine; earlier "master-quality" assets were 100s of MB to multiple GB.

---

## 12. Empirical data from the original investigation

Asset: 2172×2006, 25 fps, 11.32 s. Dark gradient column at `X=40`, frame 125. Master = lossless QuickTime Animation, 8-bit `rgb24`, full-range, well-dithered.

| build                                         | codec / notes             | range   | size      | distinct levels | longest flat run              |
| --------------------------------------------- | ------------------------- | ------- | --------- | --------------- | ----------------------------- |
| Master (`.mov`)                               | qtrle lossless, has grain | full    | 3.5 GB    | 121             | 5 px                          |
| V19 (prior webm)                              | VP9 8-bit, near-lossless  | limited | 595 MB    | —               | banded in VLC                 |
| VP9 CRF22                                     | plain                     | limited | 2.3 MB    | 106             | 330 px                        |
| VP9 CRF22                                     | plain                     | full    | 2.9 MB    | 117             | 334 px                        |
| VP9 CRF12                                     | plain (temporal grain)    | full    | 27 MB     | 118             | 41 px                         |
| VP9 CRF8                                      | plain (temporal grain)    | full    | 84 MB     | 121             | 11 px                         |
| VP9 CRF4                                      | plain (temporal grain)    | full    | 139 MB    | 123             | 5 px                          |
| static dither, fine, CRF20                    | opacity 0.10              | full    | 4.3 MB    | 115             | 210 px                        |
| static dither, fine, CRF10                    | opacity 0.10              | full    | 55 MB     | 119             | 14 px                         |
| static dither, **coarse σ1.0**, CRF16         | opacity 0.20              | full    | 17 MB     | 120             | 16 px (**too grainy per QA**) |
| **static dither, fine, shadow-masked, CRF12** | opacity 0.10, mask<170    | full    | **42 MB** | 118             | **33 px (final)**             |

Grain (stddev in flat patches): master near-black 1.5 / dark 1.6; coarse-final 0.9 / 1.1; **fine-masked-final 0.7 / 0.9** (cleaner than master).

Takeaways:

- Full range > limited range for shallow gradients (distinct levels 117 vs 106 at CRF22).
- Plain VP9 only reaches band-free by keeping expensive **temporal** grain (CRF 8 ≈ 84 MB).
- **Static** dither reaches band-free far cheaper, but **must be fine, not coarse**, to stay invisible.

---

## 13. Gotchas & environment limitations

- **`-loop 1` on an image input never ends by itself.** You MUST terminate it or ffmpeg encodes forever (we produced a "4-minute" file from an 11 s source this way). Use `blend=...:shortest=1` **and** `-shortest` on the output; optionally hard-cap with `-frames:v <duration*fps>`.
- **`select` then `crop` in one filtergraph** can throw `Invalid too big or non positive size for width '0'` (filter reinit bug). Work around: extract the full frame (`format=gray`) and index in Python (§7.3).
- **ffmpeg interactive mode hijacks stdin.** If a foreground ffmpeg is running and you type another command, its characters go to ffmpeg (a stray `c` opens "Enter command", `q` quits). Don't send new commands into a busy terminal; run encodes to completion or kill with `pkill -9 ffmpeg`.
- **This macOS ffmpeg build lacks `zscale` and `drawtext`,** and its `qtrle` decoder outputs `rgb24` (it will not surface 16-bit even if present) — verify bit depth from the container (§7.2), not the decoded pix_fmt.
- **No numpy/PIL** in the environment used — the Python snippets here use only the stdlib (`array`/`bytes`).
- **Downscaled screenshots hide banding.** Judge on native 1:1 crops, the flat-run metric, and VLC.
- **PNG-size grain heuristic** is convenient but unreliable for banding when the crop is scaled; use flat-run.

---

## 14. Glossary

- **Banding / posterization** — visible discrete steps in what should be a smooth gradient.
- **Dither** — small structured/random perturbation added so quantization steps blend perceptually. Kills banding.
- **Grain** — film-like noise; fine temporal grain acts as dither but is expensive to encode and, if strong/coarse, visible.
- **Static dither** — a _fixed_ (non-moving) noise pattern; cheap for inter-frame codecs to carry, so it survives compression.
- **Full range (`pc`) vs limited range (`tv`)** — luma 0–255 vs 16–235. Full range preserves more code values.
- **VP9 Profile 0** — 8-bit 4:2:0; the universal, correct target for Android `TextureView`.
- **`TextureView`** — Android view that samples video into an 8-bit RGB GL texture; respects Compose alpha (unlike `SurfaceView`).
- **CRF** — quality target for VP9 (`-crf` with `-b:v 0`); lower = higher quality/larger.
- **`grainmerge`** — ffmpeg blend mode computing `A + B − 128`; used to add a centered dither pattern.
- **`maskedmerge`** — ffmpeg filter blending two streams per-pixel by a third mask stream; used to confine dither to shadows.
- **Longest flat run** — the banding metric: longest run of identical luma values down a gradient column.
- **Film-grain synthesis** — codec feature (AV1, not VP9) that re-adds grain at decode; would let you encode clean + dither on playback, but Android support is inconsistent.

---

_End of playbook. If you are an agent applying this to a new asset: start at §1, use §7 to measure, iterate with §8, and confirm with the §9 checklist. Keep the encode at VP9 Profile 0 / full-range / smoothing-off, and prefer fine + shadow-masked static dither._
