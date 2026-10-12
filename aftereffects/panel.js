"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const STORAGE = {codec: "pwe.codec", settings: "pwe.settings", presets: "pwe.presets", folder: "pwe.folder"};
const STANDARD_KEYS = {
  vp9: ["bitrate_kbps", "auto_alt_ref", "arnr_maxframes", "aq_mode", "row_mt", "tile_columns"],
  av1: ["preset", "tune", "fgs_enabled", "fgs_level", "fgs_denoise"],
};
const FIELDS = [
  {key: "mode", label: "Quality mode", type: "select", options: [["crf", "Manual CRF"], ["target", "Target size"]],
   help: "Target size tries complete encodes and keeps the lowest tested CRF that fits. It changes only CRF and forces bitrate to zero."},
  {key: "crf", label: (state) => state.mode === "target" ? "Initial CRF" : "CRF", type: "int",
   min: (codec) => codec === "av1" ? 1 : 0, max: 63,
   help: "Constant rate factor: lower is higher quality and larger. In target mode this is only the first trial."},
  {key: "target_mb", label: "Target size (MB)", type: "text", visible: (state) => state.mode === "target",
   help: "Hard cap in decimal megabytes: 1 MB = 1,000,000 bytes, including WebM overhead. If impossible, the smallest tested video is delivered."},
  {key: "full_range", label: "Output range", type: "select", options: [[true, "Full (0-255)"], [false, "Limited (16-235)"]],
   help: "Controls RGB-to-YUV conversion and range metadata together. Both use a Rec.709 matrix and tags."},
  {key: "fgs_enabled", codec: "av1", label: "Film grain synthesis", type: "bool",
   help: "The AV1 decoder regenerates fine grain at playback, breaking up 8-bit banding while keeping the encode small."},
  {key: "fgs_level", codec: "av1", label: "Film grain level", type: "int", min: 0, max: 50,
   enabled: (state) => state.fgs_enabled, help: "Strength of the synthesized grain, 0-50. Zero disables synthesis."},
  {key: "bitrate_kbps", codec: "vp9", label: "Bitrate (kb/s, 0 = constant quality)", type: "int", min: 0, max: 1000000,
   enabled: (state) => state.mode !== "target", help: "A nonzero bitrate constrains quality; it is not an exact size cap. Target size always uses zero."},
  {key: "auto_alt_ref", codec: "vp9", label: "Alternate reference frames", type: "bool",
   help: "Hidden alternate reference frames can improve compression but temporal filtering can change fine grain."},
  {key: "arnr_maxframes", codec: "vp9", label: "Temporal filter frames", type: "int", min: 0, max: 15,
   help: "Alternate-reference temporal filtering window, 0-15 frames. Zero disables filtering."},
  {key: "aq_mode", codec: "vp9", label: "Adaptive quantization", type: "select",
   options: [[0, "0 - Off"], [1, "1 - Variance"], [2, "2 - Complexity"], [3, "3 - Cyclic refresh"], [4, "4 - Equator360"]],
   help: "Redistributes quantization across the image. Other modes can change gradients and grain."},
  {key: "row_mt", codec: "vp9", label: "Row multithreading", type: "bool", help: "Parallel row encoding."},
  {key: "tile_columns", codec: "vp9", label: "Tile columns (log2)", type: "int", min: 0, max: 6,
   help: "0 = 1, 1 = 2, 2 = 4 tile columns. libvpx limits this according to frame width."},
  {key: "preset", codec: "av1", label: "Encoder preset", type: "int", min: 0, max: 13,
   help: "SVT-AV1 speed/quality preset, 0 (slowest, smallest) to 13 (fastest)."},
  {key: "tune", codec: "av1", label: "Visual tuning", type: "select",
   options: [[0, "0 - Visual quality (VQ)"], [1, "1 - PSNR"], [2, "2 - SSIM"]],
   help: "VQ prioritizes perceptual quality; SSIM can suit smooth gradients."},
  {key: "fgs_denoise", codec: "av1", label: "Film grain denoise source", type: "bool", enabled: (state) => state.fgs_enabled,
   help: "Denoise the source before analyzing grain. Leave off for clean renders."},
  {key: "threads", label: "Threads (0 = auto)", type: "int", min: 0, max: 256,
   help: "Maximum encoder threads. Zero lets the encoder choose."},
  {key: "gop", label: "Max keyframe distance (0 = auto)", type: "int", min: 0, max: 1000000,
   help: "Maximum frames between keyframes. Shorter distances improve seeking but can increase size."},
  {key: "export_frames", label: "Export first and last frames (WebP)", type: "bool",
   help: "After the WebM is verified, also save its first and last frames as WebP next to it."},
  {key: "frame_quality", label: "Frame WebP quality (100 = lossless)", type: "int", min: 0, max: 100,
   enabled: (state) => state.export_frames, help: "libwebp quality from 0 (smallest) to 100 (lossless)."},
];

const $ = (id) => document.getElementById(id);
let defaults = null;
let builtins = {};
let userPresets = load(STORAGE.presets, {});
let codec = localStorage.getItem(STORAGE.codec) === "av1" ? "av1" : "vp9";
let states = null;
let comp = null;
let encoder = null;
let busy = false;
let cancelRequested = false;
let lastOutput = null;

function load(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key));
    return value && typeof value === "object" ? value : fallback;
  } catch (error) {
    return fallback;
  }
}

function extensionRoot() {
  const uri = decodeURI(window.__adobe_cep__.getSystemPath("extension"));
  return uri.replace(process.platform === "win32" ? /^file:\/\/\// : /^file:\/\//, "");
}

function encoderPath() {
  const name = process.platform === "win32" ? "png-webm-encoder.exe" : "png-webm-encoder";
  return process.env.PNG_WEBM_ENCODER || path.join(extensionRoot(), "bin", "png-webm-encoder", name);
}

function evalHost(script) {
  return new Promise((resolve) => window.__adobe_cep__.evalScript(script, (raw) => {
    try {
      resolve(JSON.parse(raw));
    } catch (error) {
      resolve({ok: false, message: `After Effects returned: ${raw}`});
    }
  }));
}

function canonical(value) {
  return JSON.stringify(Object.keys(value).sort().reduce((sorted, key) => {
    sorted[key] = value[key];
    return sorted;
  }, {}));
}

function megabytesToBytes(text) {
  const value = Number(String(text).trim());
  return Number.isFinite(value) && value > 0 ? Math.floor(value * 1000000 + 1e-6) : NaN;
}

function fpsText(rate) {
  for (const numerator of [24000, 30000, 48000, 60000, 120000]) {
    if (Math.abs(rate - numerator / 1001) < 0.0005) {
      return `${numerator}/1001`;
    }
  }
  return Math.abs(rate - Math.round(rate)) < 0.0005 ? String(Math.round(rate)) : String(Math.round(rate * 1000) / 1000);
}

function initialState(codecName) {
  return Object.assign({}, defaults[codecName], {mode: "crf", target_mb: "10"});
}

function saveState() {
  localStorage.setItem(STORAGE.codec, codec);
  localStorage.setItem(STORAGE.settings, JSON.stringify(states));
}

function standardSettings() {
  const state = states[codec];
  const data = {codec, crf: state.crf, full_range: state.full_range,
                target_bytes: state.mode === "target" ? megabytesToBytes(state.target_mb) : null};
  for (const key of STANDARD_KEYS[codec]) {
    data[key] = state[key];
  }
  return data;
}

function allPresets() {
  return Object.assign({}, builtins, userPresets);
}

function renderFields() {
  const state = states[codec];
  const container = $("fields");
  container.textContent = "";
  for (const field of FIELDS) {
    if (field.codec && field.codec !== codec) {
      continue;
    }
    const label = document.createElement("label");
    label.title = field.help;
    const caption = document.createElement("span");
    caption.textContent = typeof field.label === "function" ? field.label(state) : field.label;
    let control;
    if (field.type === "select") {
      control = document.createElement("select");
      for (const [value, text] of field.options) {
        const option = document.createElement("option");
        option.value = JSON.stringify(value);
        option.textContent = text;
        control.appendChild(option);
      }
      control.value = JSON.stringify(state[field.key]);
      control.addEventListener("change", () => update(field.key, JSON.parse(control.value)));
    } else if (field.type === "bool") {
      label.className = "check";
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = Boolean(state[field.key]);
      control.addEventListener("change", () => update(field.key, control.checked));
    } else {
      control = document.createElement("input");
      control.type = field.type === "int" ? "number" : "text";
      control.value = state[field.key];
      if (field.type === "int") {
        control.min = typeof field.min === "function" ? field.min(codec) : field.min;
        control.max = field.max;
        control.step = 1;
        control.addEventListener("change", () => {
          const value = Math.round(Number(control.value));
          const clamped = Math.min(Number(control.max), Math.max(Number(control.min), Number.isFinite(value) ? value : 0));
          control.value = clamped;
          update(field.key, clamped);
        });
      } else {
        control.addEventListener("change", () => update(field.key, control.value.trim()));
      }
    }
    control.disabled = busy || (field.enabled ? !field.enabled(state) : false);
    if (field.type === "bool") {
      label.append(control, caption);
    } else {
      label.append(caption, control);
    }
    label.hidden = field.visible ? !field.visible(state) : false;
    container.appendChild(label);
  }
  for (const radio of document.querySelectorAll("input[name=codec]")) {
    radio.checked = radio.value === codec;
  }
  $("frame-names").hidden = !state.export_frames;
  refreshPresetSelection();
}

function update(key, value) {
  states[codec][key] = value;
  saveState();
  renderFields();
}

function refreshPresetList(selected) {
  const select = $("preset");
  select.textContent = "";
  for (const name of ["Custom", ...Object.keys(builtins), ...Object.keys(userPresets).sort()]) {
    const option = document.createElement("option");
    option.value = option.textContent = name;
    select.appendChild(option);
  }
  select.value = selected || "Custom";
  refreshPresetSelection();
}

function refreshPresetSelection() {
  const select = $("preset");
  const current = canonical(standardSettings());
  const presets = allPresets();
  if (!(select.value in presets) || canonical(presets[select.value]) !== current) {
    select.value = Object.keys(presets).find((name) => canonical(presets[name]) === current) || "Custom";
  }
  const editable = select.value in userPresets && !busy;
  $("preset-rename").disabled = !editable;
  $("preset-delete").disabled = !editable;
  $("preset-save").disabled = busy;
  select.disabled = busy;
}

function applyPreset(name) {
  const data = allPresets()[name];
  if (!data) {
    return;
  }
  codec = data.codec === "av1" ? "av1" : "vp9";
  const state = states[codec];
  for (const key of Object.keys(data)) {
    if (key in state && key !== "codec") {
      state[key] = data[key];
    }
  }
  if ("target_bytes" in data) {
    state.mode = data.target_bytes === null ? "crf" : "target";
    if (data.target_bytes !== null) {
      state.target_mb = String(data.target_bytes / 1000000);
    }
  }
  saveState();
  renderFields();
  $("preset").value = name;
  refreshPresetSelection();
}

function ask(message, initial) {
  const input = $("ask-input");
  $("ask-message").textContent = message;
  input.hidden = initial === undefined;
  input.value = initial || "";
  $("ask").hidden = false;
  if (!input.hidden) {
    input.focus();
    input.select();
  }
  return new Promise((resolve) => {
    const finish = (answer) => {
      $("ask").hidden = true;
      $("ask-ok").onclick = $("ask-cancel").onclick = input.onkeydown = null;
      resolve(answer);
    };
    $("ask-ok").onclick = () => finish(input.hidden ? true : input.value.trim());
    $("ask-cancel").onclick = () => finish(null);
    input.onkeydown = (event) => {
      if (event.key === "Enter") {
        $("ask-ok").onclick();
      } else if (event.key === "Escape") {
        finish(null);
      }
    };
  });
}

async function validPresetName(name, current) {
  if (!name || name === current) {
    return false;
  }
  if (name === "Custom" || name in builtins) {
    showPhase(`"${name}" is reserved and cannot be used as a preset name.`, true);
    return false;
  }
  return !(name in userPresets) || await ask(`A preset named "${name}" already exists. Overwrite it?`) === true;
}

async function savePreset() {
  const name = await ask("Preset name:", "");
  if (await validPresetName(name)) {
    userPresets[name] = standardSettings();
    localStorage.setItem(STORAGE.presets, JSON.stringify(userPresets));
    refreshPresetList(name);
  }
}

async function renamePreset() {
  const current = $("preset").value;
  const name = await ask("New preset name:", current);
  if (current in userPresets && await validPresetName(name, current)) {
    userPresets[name] = userPresets[current];
    delete userPresets[current];
    localStorage.setItem(STORAGE.presets, JSON.stringify(userPresets));
    refreshPresetList(name);
  }
}

async function deletePreset() {
  const current = $("preset").value;
  if (current in userPresets && await ask(`Delete preset "${current}"?`) === true) {
    delete userPresets[current];
    localStorage.setItem(STORAGE.presets, JSON.stringify(userPresets));
    refreshPresetList("Custom");
  }
}

function safeName(text) {
  return text.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").replace(/^[\s._-]+|[\s._-]+$/g, "") || "output";
}

async function refreshComp() {
  const info = await evalHost("pweCompInfo()");
  const warning = $("comp-warning");
  warning.hidden = true;
  if (!info.ok) {
    comp = null;
    $("comp-name").textContent = "No composition";
    $("comp-details").textContent = info.message;
    return null;
  }
  const changed = !comp || comp.name !== info.name;
  comp = info;
  const depth = info.bitsPerChannel >= 16 ? 16 : 8;
  $("comp-name").textContent = info.name;
  $("comp-details").textContent =
    `${info.width} x ${info.height} | ${fpsText(info.frameRate)} fps | ${info.frames} frames (work area) | renders ${depth}-bit RGB PNG`;
  const problems = [];
  if (info.width % 2 || info.height % 2) {
    problems.push("Composition width and height must be even.");
  }
  const space = String(info.workingSpace || "").toLowerCase();
  if (space && space !== "none" && !/srgb|709|61966/.test(space)) {
    problems.push(`Working space is "${info.workingSpace}". Export expects SDR sRGB / Rec.709.`);
  }
  if (problems.length) {
    warning.textContent = problems.join(" ");
    warning.hidden = false;
  }
  if (!$("folder").value) {
    $("folder").value = localStorage.getItem(STORAGE.folder) || info.projectFolder || path.join(os.homedir(), "Desktop");
  }
  if (changed && !busy) {
    const stem = safeName(info.name);
    $("name").value = stem;
    $("first-name").value = `${stem}_first_frame`;
    $("last-name").value = `${stem}_last_frame`;
  }
  return info;
}

function showPhase(text, error) {
  $("phase").textContent = text;
  $("phase").classList.toggle("error", Boolean(error));
}

function setProgress(percent) {
  const bar = $("progress");
  if (percent < 0) {
    bar.removeAttribute("value");
  } else {
    bar.value = percent;
  }
}

function setBusy(value) {
  busy = value;
  for (const id of ["export", "refresh", "browse", "folder", "name", "first-name", "last-name"]) {
    $(id).disabled = value;
  }
  for (const radio of document.querySelectorAll("input[name=codec]")) {
    radio.disabled = value;
  }
  $("cancel").disabled = !value;
  renderFields();
}

function removeTree(target) {
  if (!fs.existsSync(target)) {
    return;
  }
  for (const entry of fs.readdirSync(target)) {
    const child = path.join(target, entry);
    if (fs.lstatSync(child).isDirectory()) {
      removeTree(child);
    } else {
      fs.unlinkSync(child);
    }
  }
  fs.rmdirSync(target);
}

function fileName(text, label, suffix) {
  const name = text.trim();
  if (!name || name === "." || name === ".." || /[\\/]/.test(name)) {
    throw new Error(`Enter a valid ${label} (no folders or path separators).`);
  }
  return name + suffix;
}

function runEncoder(args, onEvent) {
  return new Promise((resolve) => {
    let child;
    try {
      child = childProcess.spawn(encoderPath(), args, {windowsHide: true});
    } catch (error) {
      resolve({code: -1, message: error.message});
      return;
    }
    encoder = child;
    child.stdin.on("error", () => {});
    let buffer = "";
    let stderr = "";
    let outcome = {};
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!line) {
          continue;
        }
        try {
          const event = JSON.parse(line);
          if (event.event === "progress" || event.event === "presets") {
            onEvent(event);
          } else {
            outcome[event.event] = event;
          }
        } catch (error) {
          stderr += `${line}\n`;
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      outcome.error = outcome.error || {message: `Could not start the bundled encoder: ${error.message}`};
      encoder = null;
      resolve(Object.assign({code: -1, stderr}, outcome));
    });
    child.on("close", (code) => {
      encoder = null;
      resolve(Object.assign({code, stderr}, outcome));
    });
  });
}

function showProgress(event) {
  showPhase(event.phase);
  setProgress(event.percent);
  if ("elapsed" in event) {
    let detail = `${event.elapsed.toFixed(0)} s elapsed | Current trial: ${(event.size / 1e6).toFixed(3)} MB`;
    if (event.best_size !== null && event.best_size !== undefined) {
      detail += ` | Best fitting: ${(event.best_size / 1e6).toFixed(3)} MB`;
    }
    $("details").textContent = detail;
  }
}

async function exportWebm() {
  if (busy) {
    return;
  }
  $("reveal").hidden = true;
  $("details").textContent = "";
  let destination;
  let frameArgs = [];
  const state = states[codec];
  try {
    const info = await refreshComp();
    if (!info) {
      throw new Error("Select a composition in the Project panel or Timeline first.");
    }
    if (info.width % 2 || info.height % 2) {
      throw new Error(`Use even composition dimensions: ${info.width} x ${info.height}.`);
    }
    if (state.mode === "target" && !(megabytesToBytes(state.target_mb) >= 1)) {
      throw new Error("Enter a positive target size in MB (1 MB = 1,000,000 bytes).");
    }
    const folder = $("folder").value.trim();
    if (!folder || !fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) {
      throw new Error("Choose an existing output folder.");
    }
    destination = path.join(folder, fileName($("name").value, "output file name", ".webm"));
    if (state.export_frames) {
      frameArgs = ["--first-frame", path.join(folder, fileName($("first-name").value, "first frame file name", ".webp")),
                   "--last-frame", path.join(folder, fileName($("last-name").value, "last frame file name", ".webp"))];
    }
    localStorage.setItem(STORAGE.folder, folder);
  } catch (error) {
    showPhase(error.message, true);
    return;
  }
  const args = ["encode", "--output", destination, "--control-stdin", ...frameArgs];
  if (fs.existsSync(destination)) {
    if (await ask(`Replace ${path.basename(destination)} after a successful export?`) !== true) {
      return;
    }
    args.push("--overwrite");
  }
  const settings = {fps: fpsText(comp.frameRate), codec, target_bytes: null};
  for (const key of Object.keys(defaults[codec])) {
    if (!(key in settings)) {
      settings[key] = state[key];
    }
  }
  args.push("--settings", JSON.stringify(settings));
  if (state.mode === "target") {
    args.push("--target-mb", state.target_mb);
  }
  cancelRequested = false;
  setBusy(true);
  showPhase("Rendering the work area in After Effects...");
  setProgress(-1);
  $("log").textContent = "";
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "png-webm-ae-"));
  try {
    const sixteen = comp.bitsPerChannel >= 16;
    const render = await evalHost(`pweRender(${JSON.stringify(temp.replace(/\\/g, "/"))}, ${sixteen})`);
    if (!render.ok) {
      throw new Error(render.message);
    }
    if (cancelRequested) {
      showPhase("Canceled; destination unchanged");
      setProgress(0);
      return;
    }
    args.splice(1, 0, "--frames", temp);
    const result = await runEncoder(args, showProgress);
    $("log").textContent = [...((result.log && result.log.lines) || []), result.stderr].join("\n").trim() || "No log output.";
    setProgress(result.done ? 100 : 0);
    if (result.done) {
      const done = result.done;
      lastOutput = done.path;
      showPhase(done.target_met ? "Export complete and verified" : "Export complete; target exceeded");
      let detail = `${(done.size / 1e6).toFixed(3)} MB (${done.size.toLocaleString()} bytes) | CRF ${done.crf} | ` +
                   `${done.trials} trial(s) | ${done.frames} frames`;
      if (done.frame_files) {
        detail += ` | WebP frames: ${done.frame_files.map((file) => path.basename(file)).join(", ")}`;
      }
      $("details").textContent = detail;
      $("reveal").hidden = false;
    } else if (result.canceled) {
      showPhase("Canceled; destination unchanged");
    } else {
      const message = (result.error && result.error.message) || `The encoder exited with code ${result.code}.`;
      showPhase("Export failed; destination unchanged", true);
      $("details").textContent = message;
    }
  } catch (error) {
    showPhase("Export failed; destination unchanged", true);
    $("details").textContent = error.message;
    setProgress(0);
  } finally {
    try {
      removeTree(temp);
    } catch (error) {
      $("details").textContent += ` (Could not remove temporary frames in ${temp}.)`;
    }
    setBusy(false);
  }
}

function cancelExport() {
  cancelRequested = true;
  $("cancel").disabled = true;
  if (encoder) {
    showPhase("Canceling...");
    encoder.stdin.write("cancel\n");
  } else {
    showPhase("Canceling after the After Effects render (press Stop in the Render Queue to stop it now)...");
  }
}

function reveal() {
  if (!lastOutput) {
    return;
  }
  if (process.platform === "win32") {
    childProcess.spawn("explorer.exe", [`/select,${lastOutput}`], {detached: true});
  } else {
    childProcess.spawn("open", ["-R", lastOutput], {detached: true});
  }
}

function browse() {
  const result = window.cep.fs.showOpenDialogEx(false, true, "Select output folder", $("folder").value);
  if (result && !result.err && result.data && result.data.length) {
    $("folder").value = result.data[0];
  }
}

async function start() {
  $("refresh").onclick = refreshComp;
  $("export").onclick = exportWebm;
  $("cancel").onclick = cancelExport;
  $("reveal").onclick = reveal;
  $("browse").onclick = browse;
  $("preset").onchange = () => applyPreset($("preset").value);
  $("preset-save").onclick = savePreset;
  $("preset-rename").onclick = renamePreset;
  $("preset-delete").onclick = deletePreset;
  $("name").onchange = () => {
    const stem = $("name").value.trim();
    $("first-name").value = `${stem}_first_frame`;
    $("last-name").value = `${stem}_last_frame`;
  };
  for (const radio of document.querySelectorAll("input[name=codec]")) {
    radio.onchange = () => {
      codec = radio.value;
      saveState();
      renderFields();
    };
  }
  window.addEventListener("focus", () => {
    if (!busy) {
      refreshComp();
    }
  });
  window.addEventListener("beforeunload", () => {
    if (encoder) {
      encoder.stdin.end();
    }
  });
  const result = await runEncoder(["presets"], (event) => {
    defaults = event.defaults;
    builtins = event.presets;
  });
  if (!defaults) {
    $("export").disabled = true;
    showPhase("The bundled encoder could not start. Reinstall the extension.", true);
    $("details").textContent = (result.error && result.error.message) || result.stderr || "";
    return;
  }
  const saved = load(STORAGE.settings, {});
  states = {
    vp9: Object.assign(initialState("vp9"), saved.vp9),
    av1: Object.assign(initialState("av1"), saved.av1),
  };
  refreshPresetList();
  renderFields();
  await refreshComp();
}

start();
