import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import * as visibility from "./widget_visibility.mjs";

// Execute the real bundle entry; Vue/DOM are doubles. Real mounting is covered
// separately in the browser, not claimed by this startup integration test.
const entry = readFileSync(
  new URL("../../jarvis_widget.bundle.js", import.meta.url),
  "utf8"
).replace(/^import[\s\S]*?;\n/gm, "");

function bootWidget(boot, initialRoute = ["List", "User"]) {
  let route = initialRoute;
  let onChange;
  let mounts = 0;
  const children = [];
  const frappe = {
    boot,
    get_route: () => route,
    router: {
      on: (_event, callback) => {
        onChange = callback;
      },
    },
  };
  const context = vm.createContext({
    ...visibility,
    frappe,
    window: { frappe },
    document: {
      createElement: () => ({ style: {} }),
      body: { appendChild: (host) => children.push(host) },
    },
    Widget: {},
    createApp: () => ({
      mount: () => {
        mounts++;
      },
    }),
  });
  vm.runInContext(entry, context);
  return {
    children,
    mounts: () => mounts,
    listening: () => !!onChange,
    navigate(nextRoute) {
      route = nextRoute;
      onChange?.();
    },
    startAgain() {
      vm.runInContext(entry, context);
    },
  };
}

test("access-hidden user never mounts a host or subscribes to route changes", () => {
  const widget = bootWidget({
    jarvis_has_access: false,
    jarvis_hide_shortcut_without_access: true,
  });
  assert.equal(widget.children.length, 0);
  assert.equal(widget.mounts(), 0);
  assert.equal(widget.listening(), false);
  widget.navigate(["Form", "User", "test"]);
  widget.startAgain();
  assert.equal(widget.children.length, 0);
});

test("enabled option retains an authorized user's normal shortcut", () => {
  const widget = bootWidget({
    jarvis_has_access: true,
    jarvis_hide_shortcut_without_access: true,
  });
  assert.equal(widget.mounts(), 1);
  assert.equal(widget.children[0].style.display, "");
  widget.navigate(["Form", "User", "test"]);
  widget.startAgain();
  assert.equal(widget.mounts(), 1);
});

test("old or disabled setting keeps the role-less user's launcher", () => {
  for (const option of [undefined, false]) {
    const widget = bootWidget({
      jarvis_has_access: false,
      jarvis_hide_shortcut_without_access: option,
    });
    assert.equal(widget.mounts(), 1);
    assert.equal(widget.children[0].style.display, "");
  }
});

test("route hiding retains the same mounted widget across navigation", () => {
  const widget = bootWidget({ jarvis_has_access: true }, ["setup-wizard"]);
  assert.equal(widget.mounts(), 1);
  assert.equal(widget.children[0].style.display, "none");
  widget.navigate(["List", "User"]);
  assert.equal(widget.children[0].style.display, "");
  widget.navigate(["jarvis-chat"]);
  assert.equal(widget.children[0].style.display, "none");
  assert.equal(widget.mounts(), 1);
});
