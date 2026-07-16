import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(root, "dxmtbench.html"), "utf8");

function extractFunction(name) {
    const start = html.indexOf(`function ${name}(`);
    assert.notEqual(start, -1, `missing ${name} in dxmtbench.html`);
    const bodyStart = html.indexOf("{", start);
    let depth = 0;
    for (let index = bodyStart; index < html.length; ++index) {
        if (html[index] === "{")
            depth++;
        else if (html[index] === "}" && --depth === 0)
            return html.slice(start, index + 1);
    }
    throw new Error(`unterminated ${name} in dxmtbench.html`);
}

function lifecycleContext(overrides = {}) {
    const posts = [];
    const events = [];
    const context = vm.createContext({
        config: {},
        emitEvent: (event, detail) => events.push({ event, detail }),
        navigator: { userAgent: "test" },
        postResult: result => posts.push(result),
        runId: "run-1",
        setMetric() {},
        state: { phase: "complete", error: "", terminalResultPosted: false },
        workloadName: "clear",
        ...overrides,
    });
    vm.runInContext(
        `${extractFunction("postTerminalResult")}\n${extractFunction("failBenchmark")}`,
        context,
    );
    return { context, events, posts };
}

test("an error during the pre-post complete phase wins the terminal result", () => {
    const { context, posts } = lifecycleContext();

    context.failBenchmark(new Error("late failure"), "unhandledrejection");

    assert.equal(context.state.phase, "error");
    assert.equal(context.state.terminalResultPosted, true);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].terminalStatus, "error");
    assert.match(posts[0].error, /late failure/);
});

test("the first terminal result remains the only result", () => {
    const { context, posts } = lifecycleContext();

    assert.equal(context.postTerminalResult({ terminalStatus: "ok" }), true);
    context.failBenchmark(new Error("too late"), "window.error");
    assert.equal(context.postTerminalResult({ terminalStatus: "error" }), false);

    assert.deepEqual(posts, [{ terminalStatus: "ok" }]);
    assert.equal(context.state.phase, "complete");
});

test("server configuration remains authoritative over query parameters", () => {
    const context = vm.createContext({
        params: new URLSearchParams("?run=stale&expectedCanvasWidth=0"),
        serverConfig: { run: "current", expectedCanvasWidth: "3840" },
        serverConfigKeys: ["expectedCanvasWidth", "run"],
    });
    vm.runInContext(extractFunction("readParam"), context);

    assert.equal(context.readParam("run", "fallback"), "current");
    assert.equal(context.readParam("expectedCanvasWidth", "0"), "3840");
    assert.equal(context.readParam("missing", "fallback"), "fallback");
});
