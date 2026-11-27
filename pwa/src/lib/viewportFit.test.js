import { test } from "node:test";
import assert from "node:assert/strict";
import { fitViewport, scrollFocusedIntoView } from "./viewportFit.js";

const fakeEl = () => {
	const style = new Map();
	return {
		style: {
			setProperty: (k, v) => style.set(k, v),
			removeProperty: (k) => style.delete(k),
		},
		get: (k) => style.get(k),
		size: () => style.size,
	};
};
const fakeWindow = (vv) => {
	const listeners = new Map();
	const visualViewport = {
		...vv,
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
	};
	return { visualViewport, listeners };
};

// Break: the sheet keeps the layout viewport's height when the iOS keyboard shrinks the visual one
// (its footer sits under the keyboard), does not follow a later resize or scroll, or leaves its
// variables behind after it closes.
test("the sheet follows the visual viewport and lets go when it closes", () => {
	const el = fakeEl();
	const win = fakeWindow({ offsetTop: 0, height: 844 });
	const release = fitViewport(el, win);
	assert.equal(el.get("--jv-vv-height"), "844px");
	win.visualViewport.height = 520.4; // the keyboard opened
	win.visualViewport.offsetTop = 12;
	win.listeners.get("resize")();
	assert.equal(el.get("--jv-vv-height"), "520px");
	assert.equal(el.get("--jv-vv-top"), "12px");
	release();
	assert.equal(el.size(), 0);
	assert.equal(win.listeners.size, 0);
});

// Break: a browser with no visualViewport throws (the sheet would not open), or sets variables that
// then override the stylesheet's own 100dvh fallback.
test("with no visual viewport nothing is set", () => {
	const el = fakeEl();
	assert.equal(fitViewport(el, {}), null);
	assert.equal(fitViewport(null, fakeWindow({ height: 1, offsetTop: 0 })), null);
	assert.equal(el.size(), 0);
});

// Break: a focused field stays behind the keyboard, or a button tap scrolls the sheet.
test("a focused text control is scrolled to the middle after the keyboard opens; others are left", () => {
	const scrolled = [];
	const control = (tagName) => ({
		tagName,
		isConnected: true,
		scrollIntoView: (o) => scrolled.push([tagName, o]),
	});
	let later;
	const defer = (fn) => (later = fn);
	assert.equal(scrollFocusedIntoView(control("INPUT"), defer), true);
	assert.deepEqual(scrolled, []);
	later();
	assert.deepEqual(scrolled, [["INPUT", { block: "center", behavior: "smooth" }]]);
	for (const tag of ["TEXTAREA", "SELECT"])
		assert.equal(scrollFocusedIntoView(control(tag), defer), true);
	for (const other of ["BUTTON", "A", "DIV", undefined])
		assert.equal(scrollFocusedIntoView(control(other), defer), false);
	assert.equal(scrollFocusedIntoView(null, defer), false);
	// a control removed meanwhile is not scrolled
	const gone = { ...control("INPUT"), isConnected: false };
	scrolled.length = 0;
	scrollFocusedIntoView(gone, defer);
	later();
	assert.deepEqual(scrolled, []);
});

// Break: the home-bar padding stays under a footer that the keyboard has lifted (dead space above
// the keyboard), or it is not given back when the keyboard closes.
test("the home-bar padding goes while the keyboard is up and comes back after", () => {
	const el = fakeEl();
	const win = fakeWindow({ offsetTop: 0, height: 844 });
	win.innerHeight = 844;
	const release = fitViewport(el, win);
	assert.equal(el.get("--jv-kb-bottom"), undefined);
	win.visualViewport.height = 520; // the keyboard opened
	win.listeners.get("resize")();
	assert.equal(el.get("--jv-kb-bottom"), "0px");
	win.visualViewport.height = 844;
	win.listeners.get("resize")();
	assert.equal(el.get("--jv-kb-bottom"), undefined);
	win.visualViewport.height = 520;
	win.listeners.get("resize")();
	release();
	assert.equal(el.size(), 0);
});
