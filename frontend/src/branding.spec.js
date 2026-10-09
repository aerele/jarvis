import { computed, nextTick } from "vue";
import { mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// admin-v2#622: a saved brand applies at once, with no reload. The boot values seed it;
// applyBranding (the Branding pane's Save) changes it in place.
async function load(boot = {}) {
	vi.resetModules();
	Object.assign(window, { agent_name: "", brand_logo_url: "", brand_favicon_url: "", ...boot });
	return import("./branding.js");
}

beforeEach(() => {
	document.head.innerHTML =
		'<link rel="icon" href="/assets/jarvis/icon.png" sizes="32x32" type="image/png">' +
		'<link rel="apple-touch-icon" href="/assets/jarvis/apple.png">';
	document.title = "Jarvis";
});
afterEach(() => {
	delete window.agent_name;
	delete window.brand_logo_url;
	delete window.brand_favicon_url;
});

const iconHrefs = () =>
	[...document.querySelectorAll('link[rel="icon"], link[rel="apple-touch-icon"]')].map((l) =>
		l.getAttribute("href")
	);

describe("brand", () => {
	it("starts from the boot values, with the Jarvis defaults for blanks", async () => {
		const { brand } = await load();
		expect(brand.agentName).toBe("Jarvis");
		expect(brand.logoUrl).toBe("");
		expect(brand.isWhitelabeled).toBe(false);

		const named = (await load({ agent_name: " Acme ", brand_logo_url: "/files/a.png" })).brand;
		expect(named.agentName).toBe("Acme");
		expect(named.logoUrl).toBe("/files/a.png");
		expect(named.isWhitelabeled).toBe(true);
	});

	it("applyBranding changes it in place, so readers update without a reload", async () => {
		const { brand, applyBranding } = await load();
		const label = computed(() => `Ask ${brand.agentName}`);
		expect(label.value).toBe("Ask Jarvis");
		applyBranding({
			agent_name: "Acme",
			brand_logo_url: "/files/b.png",
			brand_favicon_url: "",
		});
		expect(label.value).toBe("Ask Acme");
		expect(brand.logoUrl).toBe("/files/b.png");
		expect(brand.isWhitelabeled).toBe(true);
		applyBranding({ agent_name: "", brand_logo_url: "", brand_favicon_url: "" });
		expect(label.value).toBe("Ask Jarvis");
		expect(brand.isWhitelabeled).toBe(false);
	});

	it("the tab title and favicon follow the brand, and come back when it is removed", async () => {
		const { applyBrandChrome, applyBranding } = await load();
		applyBrandChrome();
		expect(document.title).toBe("Jarvis");
		expect(iconHrefs()).toEqual(["/assets/jarvis/icon.png", "/assets/jarvis/apple.png"]);

		applyBranding({
			agent_name: "Acme",
			brand_logo_url: "",
			brand_favicon_url: "/files/fav.ico",
		});
		expect(document.title).toBe("Acme Chat");
		expect(iconHrefs()).toEqual(["/files/fav.ico", "/files/fav.ico"]);

		applyBranding({ agent_name: "", brand_logo_url: "", brand_favicon_url: "" });
		expect(document.title).toBe("Jarvis");
		expect(iconHrefs()).toEqual(["/assets/jarvis/icon.png", "/assets/jarvis/apple.png"]);
		const icon = document.querySelector('link[rel="icon"]');
		expect([icon.getAttribute("sizes"), icon.getAttribute("type")]).toEqual([
			"32x32",
			"image/png",
		]);
	});

	it("JarvisMark shows a new logo as soon as it is saved", async () => {
		const { applyBranding } = await load();
		const { default: JarvisMark } = await import("./components/JarvisMark.vue");
		const w = mount(JarvisMark);
		expect(w.find("img").exists()).toBe(false);
		applyBranding({
			agent_name: "",
			brand_logo_url: "/files/acme.png",
			brand_favicon_url: "",
		});
		await nextTick();
		expect(w.find("img").attributes("src")).toBe("/files/acme.png");
	});
});
