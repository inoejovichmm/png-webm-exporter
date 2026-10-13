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

function pweRender(folder) {
    var queue = app.project.renderQueue;
    var paused = [];
    var item = null;
    var renderComp = null;
    var matteSource = null;
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
        renderComp = comp.duplicate();
        var matte = renderComp.layers.addSolid(comp.bgColor, "PNG WebM Matte", comp.width, comp.height,
            comp.pixelAspect, comp.duration);
        matteSource = matte.source;
        matte.moveToEnd();
        item = queue.items.add(renderComp);
        item.timeSpanStart = comp.workAreaStart;
        item.timeSpanDuration = comp.workAreaDuration;
        if (!pweUsePngSequence(item)) {
            throw new Error("No PNG Sequence Output Module template has a depth supported by this project. Create " +
                "one with Format: PNG Sequence and matching color depth (Edit > Templates > Output Module).");
        }
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
        if (renderComp !== null) {
            try {
                renderComp.remove();
            } catch (ignored) { }
        }
        if (matteSource !== null) {
            try {
                matteSource.remove();
            } catch (ignored) { }
        }
        for (var restore = 0; restore < paused.length; restore++) {
            try {
                paused[restore].render = true;
            } catch (ignored) { }
        }
    }
}
