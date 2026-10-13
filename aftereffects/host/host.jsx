// ExtendScript (ES3) side of the PNG to WebM panel. Every entry point returns a JSON string.

var pweRenderState = null;
var pweStopRequested = false;
var pwePreparationResult = null;

function pweJson(value) {
    if (value === null || value === undefined) {
        return "null";
    }
    var type = typeof value;
    if (type === "number") {
        return isFinite(value) ? String(value) : "null";
    }
    if (type === "boolean") {
        return value ? "true" : "false";
    }
    if (type === "string") {
        return "\"" + value.replace(/[\\"\u0000-\u001f\u2028\u2029]/g, function (character) {
            return "\\u" + ("0000" + character.charCodeAt(0).toString(16)).slice(-4);
        }) + "\"";
    }
    var parts = [];
    if (value instanceof Array) {
        for (var index = 0; index < value.length; index++) {
            parts.push(pweJson(value[index]));
        }
        return "[" + parts.join(",") + "]";
    }
    for (var key in value) {
        if (value.hasOwnProperty(key)) {
            parts.push(pweJson(key) + ":" + pweJson(value[key]));
        }
    }
    return "{" + parts.join(",") + "}";
}

function pweActiveComp() {
    var item = app.project ? app.project.activeItem : null;
    if (!(item instanceof CompItem)) {
        throw new Error("Select a composition in the Project panel or open one in the Timeline.");
    }
    return item;
}

function pweCompInfo() {
    try {
        var comp = pweActiveComp();
        var workingSpace = "";
        try {
            workingSpace = app.project.workingSpace;
        } catch (ignored) { }
        return pweJson({
            ok: true,
            name: comp.name,
            width: comp.width,
            height: comp.height,
            frameRate: comp.frameRate,
            frames: Math.round(comp.workAreaDuration * comp.frameRate),
            bitsPerChannel: app.project.bitsPerChannel,
            workingSpace: workingSpace,
            projectFolder: app.project.file ? app.project.file.parent.fsName : ""
        });
    } catch (error) {
        return pweJson({ ok: false, message: error.toString() });
    }
}

function pweSetting(item, key) {
    try {
        return item.outputModule(1).getSettings(GetSettingsFormat.STRING)[key];
    } catch (ignored) {
        return "";
    }
}

function pwePngTemplateScore(item, maximumDepth) {
    var settings = item.outputModule(1).getSettings(GetSettingsFormat.STRING);
    if (settings["Format"] !== "PNG Sequence") {
        return -1;
    }
    var depth = /Trillions of Colors/.test(settings["Depth"] || "") ? 16 :
        (/Millions of Colors/.test(settings["Depth"] || "") ? 8 : 0);
    if (!depth || depth > maximumDepth) {
        return -1;
    }
    return (settings["Channels"] === "RGB" ? 100 : 0) + depth;
}

function pweUsePngSequence(item) {
    var maximumDepth = app.project.bitsPerChannel >= 16 ? 16 : 8;
    var templates = item.outputModule(1).templates;
    var bestTemplate = null;
    var bestScore = -1;
    for (var index = 0; index < templates.length; index++) {
        try {
            item.outputModule(1).applyTemplate(templates[index]);
        } catch (ignored) {
            continue;
        }
        var score = pwePngTemplateScore(item, maximumDepth);
        if (score > bestScore) {
            bestScore = score;
            bestTemplate = templates[index];
        }
    }
    if (bestTemplate === null) {
        return false;
    }
    item.outputModule(1).applyTemplate(bestTemplate);
    return pwePngTemplateScore(item, maximumDepth) >= 0;
}

function pweCleanupRender() {
    if (pweRenderState === null) {
        return;
    }
    var state = pweRenderState;
    pweRenderState = null;
    pweStopRequested = false;
    if (state.stopTask !== null) {
        try {
            app.cancelTask(state.stopTask);
        } catch (ignored) { }
    }
    if (state.preparationTask !== null) {
        try {
            app.cancelTask(state.preparationTask);
        } catch (ignored) { }
    }
    if (state.startTask !== null) {
        try {
            app.cancelTask(state.startTask);
        } catch (ignored) { }
    }
    if (state.item !== null) {
        try {
            state.item.remove();
        } catch (ignored) { }
    }
    if (state.renderComp !== null) {
        try {
            state.renderComp.remove();
        } catch (ignored) { }
    }
    if (state.matteSource !== null) {
        try {
            state.matteSource.remove();
        } catch (ignored) { }
    }
    for (var index = 0; index < state.paused.length; index++) {
        try {
            state.paused[index].render = true;
        } catch (ignored) { }
    }
}

function pweStopIfRequested() {
    if (!pweStopRequested || pweRenderState === null || !pweRenderState.ready) {
        return;
    }
    try {
        if (app.project.renderQueue.rendering) {
            app.project.renderQueue.stopRendering();
        }
    } catch (ignored) { }
}

function pweSchedulePrepareStep() {
    if (pweRenderState !== null) {
        pweRenderState.preparationTask = app.scheduleTask("pwePrepareRenderStep()", 10, false);
    }
}

function pwePrepareRenderStep() {
    var state = pweRenderState;
    if (state === null) {
        return;
    }
    state.preparationTask = null;
    if (pweStopRequested) {
        pwePreparationResult = { ok: true, canceled: true };
        pweCleanupRender();
        return;
    }
    try {
        var queue = app.project.renderQueue;
        if (state.stage === 0) {
            state.comp = pweActiveComp();
            if (queue.rendering) {
                throw new Error("The Render Queue is busy. Wait for it to finish and try again.");
            }
            for (var index = 1; index <= queue.numItems; index++) {
                if (queue.item(index).status === RQItemStatus.QUEUED) {
                    queue.item(index).render = false;
                    state.paused.push(queue.item(index));
                }
            }
            state.phase = "Duplicating composition";
            state.percent = 20;
            state.stage = 1;
            pweSchedulePrepareStep();
            return;
        }
        if (state.stage === 1) {
            state.renderComp = state.comp.duplicate();
            state.phase = "Flattening transparency";
            state.percent = 45;
            state.stage = 2;
            pweSchedulePrepareStep();
            return;
        }
        if (state.stage === 2) {
            var matte = state.renderComp.layers.addSolid(state.comp.bgColor, "PNG WebM Matte", state.comp.width,
                state.comp.height, state.comp.pixelAspect, state.comp.duration);
            state.matteSource = matte.source;
            matte.moveToEnd();
            state.phase = "Configuring PNG output";
            state.percent = 65;
            state.stage = 3;
            pweSchedulePrepareStep();
            return;
        }
        if (state.stage === 3) {
            state.item = queue.items.add(state.renderComp);
            state.item.timeSpanStart = state.comp.workAreaStart;
            state.item.timeSpanDuration = state.comp.workAreaDuration;
            if (!pweUsePngSequence(state.item)) {
                throw new Error("No PNG Sequence Output Module template has a depth supported by this project. Create " +
                    "one with Format: PNG Sequence and matching color depth (Edit > Templates > Output Module).");
            }
            state.item.outputModule(1).postRenderAction = PostRenderAction.NONE;
            state.item.outputModule(1).file = new File(state.folder + "/frame_[#####].png");
            state.queueIndex = queue.numItems;
            queue.showWindow(true);
            state.stopTask = app.scheduleTask("pweStopIfRequested()", 100, true);
            state.phase = "Ready to render";
            state.percent = 100;
            state.ready = true;
        }
    } catch (error) {
        pwePreparationResult = { ok: false, message: error.toString() };
        pweCleanupRender();
    }
}

function pwePrepareRender(folder) {
    if (pweRenderState !== null) {
        return pweJson({ ok: false, message: "An After Effects render is already active." });
    }
    pweStopRequested = false;
    pwePreparationResult = null;
    pweRenderState = {
        folder: folder, paused: [], item: null, renderComp: null, matteSource: null,
        stopTask: null, preparationTask: null, startTask: null, stage: 0, ready: false,
        renderStarted: false, renderFinished: false, renderDone: false, renderCanceled: false,
        renderError: null, phase: "Starting", percent: 5
    };
    try {
        pweSchedulePrepareStep();
        return pweJson({ ok: true, preparing: true, phase: pweRenderState.phase, percent: pweRenderState.percent });
    } catch (error) {
        pwePreparationResult = { ok: false, message: error.toString() };
        pweCleanupRender();
        return pweJson(pwePreparationResult);
    }
}

function pweGetRenderStatus() {
    if (pwePreparationResult !== null) {
        return pweJson(pwePreparationResult);
    }
    if (pweRenderState === null) {
        return pweJson({ ok: false, message: "No render preparation is active." });
    }
    var itemStatus = pweRenderState.item === null ? null : pweRenderState.item.status;
    var renderDone = itemStatus === RQItemStatus.DONE;
    var renderCanceled = pweStopRequested || itemStatus === RQItemStatus.USER_STOPPED ||
        (itemStatus === RQItemStatus.UNQUEUED && pweRenderState.ready);
    var renderError = itemStatus === RQItemStatus.ERR_STOPPED ? "The Render Queue stopped because of an error." : null;
    var renderFinished = renderDone || renderCanceled || renderError !== null;
    return pweJson({
        ok: true, preparing: !pweRenderState.ready, ready: pweRenderState.ready, phase: pweRenderState.phase,
        percent: pweRenderState.percent, queueIndex: pweRenderState.queueIndex,
        renderStarted: itemStatus === RQItemStatus.RENDERING || renderDone || itemStatus === RQItemStatus.USER_STOPPED ||
            itemStatus === RQItemStatus.ERR_STOPPED,
        rendering: app.project.renderQueue.rendering, renderFinished: renderFinished,
        renderDone: renderDone, renderCanceled: renderCanceled, renderError: renderError
    });
}

function pweStopRender() {
    try {
        pweStopRequested = true;
        if (app.project.renderQueue.rendering) {
            app.project.renderQueue.stopRendering();
        } else if (pweRenderState !== null && pweRenderState.item !== null &&
            pweRenderState.item.status === RQItemStatus.QUEUED) {
            pweRenderState.item.render = false;
        }
        return pweJson({ ok: true, requested: true });
    } catch (error) {
        return pweJson({ ok: false, message: error.toString() });
    }
}

function pweFinishRender() {
    pweCleanupRender();
    pwePreparationResult = null;
    return pweJson({ ok: true });
}
