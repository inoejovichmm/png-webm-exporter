// ExtendScript (ES3) side of the PNG to WebM panel. Every entry point returns a JSON string.

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

function pweTrySettings(item, settings) {
    // setSettings invalidates the OutputModule object, so always fetch it again.
    try {
        item.outputModule(1).setSettings(settings);
    } catch (ignored) { }
}

function pweUsePngSequence(item) {
    pweTrySettings(item, { "Format": "PNG Sequence" });
    if (pweSetting(item, "Format") === "PNG Sequence") {
        return true;
    }
    var templates = item.outputModule(1).templates;
    for (var index = 0; index < templates.length; index++) {
        try {
            item.outputModule(1).applyTemplate(templates[index]);
        } catch (ignored) {
            continue;
        }
        if (pweSetting(item, "Format") === "PNG Sequence") {
            return true;
        }
    }
    return false;
}

function pweRender(folder, sixteenBit) {
    var queue = app.project.renderQueue;
    var paused = [];
    var item = null;
    try {
        var comp = pweActiveComp();
        if (queue.rendering) {
            throw new Error("The Render Queue is busy. Wait for it to finish and try again.");
        }
        for (var index = 1; index <= queue.numItems; index++) {
            if (queue.item(index).status === RQItemStatus.QUEUED) {
                queue.item(index).render = false;
                paused.push(queue.item(index));
            }
        }
        item = queue.items.add(comp);
        item.timeSpanStart = comp.workAreaStart;
        item.timeSpanDuration = comp.workAreaDuration;
        if (!pweUsePngSequence(item)) {
            throw new Error("After Effects did not allow selecting PNG Sequence from a script. Create an Output " +
                "Module template with Format: PNG Sequence (Edit > Templates > Output Module) and retry.");
        }
        pweTrySettings(item, { "Video Output": true });
        pweTrySettings(item, { "Channels": "RGB" });
        pweTrySettings(item, { "Depth": sixteenBit ? "Trillions of Colors" : "Millions of Colors" });
        pweTrySettings(item, { "Output Audio": "Off" });
        item.outputModule(1).postRenderAction = PostRenderAction.NONE;
        item.outputModule(1).file = new File(folder + "/frame_[#####].png");
        queue.render();
        if (item.status !== RQItemStatus.DONE) {
            throw new Error("The render did not finish (it was stopped or failed in the Render Queue).");
        }
        return pweJson({ ok: true, channels: pweSetting(item, "Channels"), depth: pweSetting(item, "Depth") });
    } catch (error) {
        return pweJson({ ok: false, message: error.toString() });
    } finally {
        if (item !== null) {
            try {
                item.remove();
            } catch (ignored) { }
        }
        for (var restore = 0; restore < paused.length; restore++) {
            try {
                paused[restore].render = true;
            } catch (ignored) { }
        }
    }
}
